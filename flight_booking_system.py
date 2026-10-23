from dataclasses import dataclass
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from threading import RLock
from typing import Callable, Optional, Sequence
import uuid
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must include a timezone")


class FlightStatus(Enum):
    SCHEDULED = "SCHEDULED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class SeatStatus(Enum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"


class BookingStatus(Enum):
    CREATED = "CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLATION_PENDING = "CANCELLATION_PENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class PaymentStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class TicketStatus(Enum):
    ISSUED = "ISSUED"
    CANCELLED = "CANCELLED"


class BookingError(Exception):
    def __init__(self, message: str, booking: Optional["Booking"] = None):
        super().__init__(message)
        self.booking = booking


class FlightNotFoundError(BookingError):
    pass


class SeatUnavailableError(BookingError):
    pass


class PaymentFailedError(BookingError):
    pass


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    city: str
    country: str
    time_zone: str = "UTC"

    def __post_init__(self) -> None:
        ZoneInfo(self.time_zone)


@dataclass(frozen=True)
class Flight:
    flight_id: str
    flight_number: str
    airline_name: str


@dataclass(frozen=True)
class SeatDefinition:
    seat_number: str
    cabin_class: str
    price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", Decimal(str(self.price)))
        if self.price < 0:
            raise ValueError("seat price cannot be negative")


@dataclass(frozen=True)
class Aircraft:
    aircraft_id: str
    model: str
    seats: tuple[SeatDefinition, ...]


@dataclass(frozen=True)
class FlightInstance:
    instance_id: str
    flight: Flight
    origin: Airport
    destination: Airport
    departure_at: datetime
    arrival_at: datetime
    aircraft_id: str
    status: FlightStatus = FlightStatus.SCHEDULED

    def __post_init__(self) -> None:
        _require_aware(self.departure_at)
        _require_aware(self.arrival_at)
        if self.origin.code == self.destination.code:
            raise ValueError("origin and destination must differ")
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival must be after departure")


@dataclass(frozen=True)
class Passenger:
    passenger_id: str
    first_name: str
    last_name: str
    email: str


@dataclass(frozen=True)
class PassengerSeat:
    passenger: Passenger
    seat_number: str


@dataclass(frozen=True)
class FareQuote:
    quote_id: str
    flight_instance_id: str
    seat_prices: tuple[tuple[str, Decimal], ...]
    total: Decimal
    created_at: datetime
    expires_at: datetime
    currency: str = "USD"

    def __post_init__(self) -> None:
        normalized = self.currency.upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "currency", normalized)


@dataclass
class Booking:
    booking_id: str
    idempotency_key: str
    request_fingerprint: tuple[object, ...]
    user_id: str
    flight_instance_id: str
    assignments: tuple[PassengerSeat, ...]
    quote: FareQuote
    status: BookingStatus = BookingStatus.CREATED
    payment_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)

    @property
    def amount(self) -> Decimal:
        return self.quote.total

    def transition(self, new_status: BookingStatus) -> None:
        allowed = {
            BookingStatus.CREATED: {BookingStatus.PAYMENT_PENDING, BookingStatus.FAILED},
            BookingStatus.PAYMENT_PENDING: {BookingStatus.CONFIRMED, BookingStatus.FAILED},
            BookingStatus.CONFIRMED: {
                BookingStatus.CANCELLATION_PENDING,
                BookingStatus.CANCELLED,
                BookingStatus.FAILED,
            },
            BookingStatus.CANCELLATION_PENDING: {BookingStatus.CANCELLED},
            BookingStatus.CANCELLED: set(),
            BookingStatus.FAILED: set(),
        }
        if new_status not in allowed[self.status]:
            raise ValueError(f"invalid booking transition: {self.status.value} -> {new_status.value}")
        self.status = new_status


@dataclass
class InventorySeat:
    flight_instance_id: str
    definition: SeatDefinition
    status: SeatStatus = SeatStatus.AVAILABLE
    reserved_by: Optional[str] = None
    hold_expires_at: Optional[datetime] = None

    def is_available(self, now: datetime) -> bool:
        return self.status == SeatStatus.AVAILABLE or (
            self.status == SeatStatus.HELD
            and self.hold_expires_at is not None
            and now >= self.hold_expires_at
        )


@dataclass
class Payment:
    payment_id: str
    booking_id: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    idempotency_key: str


@dataclass
class Ticket:
    ticket_id: str
    booking_id: str
    flight_instance_id: str
    passenger_id: str
    seat_number: str
    status: TicketStatus = TicketStatus.ISSUED


@dataclass(frozen=True)
class Notification:
    event_type: str
    booking_id: str
    message: str


class FlightRepository:
    def __init__(self) -> None:
        self._flights: dict[str, FlightInstance] = {}

    def add(self, flight: FlightInstance) -> None:
        self._flights[flight.instance_id] = flight

    def get(self, instance_id: str) -> Optional[FlightInstance]:
        return self._flights.get(instance_id)

    def search(
        self,
        origin_code: str,
        destination_code: str,
        departure_date: date,
    ) -> list[FlightInstance]:
        matches = [
            flight
            for flight in self._flights.values()
            if flight.status == FlightStatus.SCHEDULED
            and flight.origin.code == origin_code.upper()
            and flight.destination.code == destination_code.upper()
            and flight.departure_at.astimezone(
                ZoneInfo(flight.origin.time_zone)
            ).date() == departure_date
        ]
        return sorted(matches, key=lambda flight: flight.departure_at)


class InventoryRepository:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._seats: dict[tuple[str, str], InventorySeat] = {}
        self._clock = clock
        self._lock = RLock()

    def add_aircraft(self, flight_instance_id: str, aircraft: Aircraft) -> None:
        with self._lock:
            for definition in aircraft.seats:
                key = (flight_instance_id, definition.seat_number)
                if key in self._seats:
                    raise ValueError(f"duplicate seat: {definition.seat_number}")
                self._seats[key] = InventorySeat(flight_instance_id, definition)

    def get_seat(
        self, flight_instance_id: str, seat_number: str
    ) -> Optional[InventorySeat]:
        with self._lock:
            return self._seats.get((flight_instance_id, seat_number))

    def available_seats(self, flight_instance_id: str) -> list[SeatDefinition]:
        now = self._clock()
        _require_aware(now)
        with self._lock:
            return [
                seat.definition
                for seat in self._seats.values()
                if seat.flight_instance_id == flight_instance_id and seat.is_available(now)
            ]

    def hold_seats(
        self,
        flight_instance_id: str,
        seat_numbers: Sequence[str],
        booking_id: str,
        duration: timedelta,
    ) -> bool:
        now = self._clock()
        _require_aware(now)
        requested = tuple(seat_numbers)
        if not requested or len(set(requested)) != len(requested):
            return False
        with self._lock:
            seats = [
                self._seats.get((flight_instance_id, seat_number))
                for seat_number in requested
            ]
            if any(seat is None or not seat.is_available(now) for seat in seats):
                return False
            for seat in seats:
                seat.status = SeatStatus.HELD
                seat.reserved_by = booking_id
                seat.hold_expires_at = now + duration
            return True

    def confirm_seats(
        self,
        flight_instance_id: str,
        seat_numbers: Sequence[str],
        booking_id: str,
    ) -> bool:
        now = self._clock()
        _require_aware(now)
        with self._lock:
            seats = [
                self._seats.get((flight_instance_id, seat_number))
                for seat_number in seat_numbers
            ]
            if any(
                seat is None
                or seat.status != SeatStatus.HELD
                or seat.reserved_by != booking_id
                or seat.hold_expires_at is None
                or now >= seat.hold_expires_at
                for seat in seats
            ):
                return False
            for seat in seats:
                seat.status = SeatStatus.CONFIRMED
                seat.hold_expires_at = None
            return True

    def can_release_seats(
        self,
        flight_instance_id: str,
        seat_numbers: Sequence[str],
        booking_id: str,
    ) -> bool:
        with self._lock:
            seats = [
                self._seats.get((flight_instance_id, seat_number))
                for seat_number in seat_numbers
            ]
            return bool(seats) and all(
                seat is not None
                and seat.reserved_by == booking_id
                and seat.status in (SeatStatus.HELD, SeatStatus.CONFIRMED)
                for seat in seats
            )

    def release_seats(
        self,
        flight_instance_id: str,
        seat_numbers: Sequence[str],
        booking_id: str,
    ) -> bool:
        with self._lock:
            seats = [
                self._seats.get((flight_instance_id, seat_number))
                for seat_number in seat_numbers
            ]
            if not seats or any(
                seat is None
                or seat.reserved_by != booking_id
                or seat.status not in (SeatStatus.HELD, SeatStatus.CONFIRMED)
                for seat in seats
            ):
                return False
            for seat in seats:
                seat.status = SeatStatus.AVAILABLE
                seat.reserved_by = None
                seat.hold_expires_at = None
            return True


class BookingRepository:
    def __init__(self) -> None:
        self._bookings: dict[str, Booking] = {}
        self._idempotency_keys: dict[str, str] = {}
        self._lock = RLock()

    def save(self, booking: Booking) -> None:
        with self._lock:
            self._bookings[booking.booking_id] = booking
            self._idempotency_keys[booking.idempotency_key] = booking.booking_id

    def get(self, booking_id: str) -> Optional[Booking]:
        with self._lock:
            return self._bookings.get(booking_id)

    def get_by_idempotency_key(self, key: str) -> Optional[Booking]:
        with self._lock:
            booking_id = self._idempotency_keys.get(key)
            return self._bookings.get(booking_id) if booking_id else None


class PaymentService:
    def __init__(
        self,
        charge_succeeds: Callable[[str, Decimal], bool] = lambda _booking, _amount: True,
    ) -> None:
        self._charge_succeeds = charge_succeeds
        self._payments_by_key: dict[str, Payment] = {}
        self._payments_by_id: dict[str, Payment] = {}
        self._lock = RLock()

    def charge(
        self,
        booking_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> Payment:
        with self._lock:
            existing = self._payments_by_key.get(idempotency_key)
            if existing:
                if (
                    existing.booking_id != booking_id
                    or existing.amount != amount
                    or existing.currency != currency
                ):
                    raise ValueError("payment idempotency key was reused with different data")
                return existing
            try:
                succeeded = self._charge_succeeds(booking_id, amount)
            except Exception:
                succeeded = False
            payment = Payment(
                payment_id=str(uuid.uuid4()),
                booking_id=booking_id,
                amount=amount,
                currency=currency,
                status=PaymentStatus.SUCCESS if succeeded else PaymentStatus.FAILED,
                idempotency_key=idempotency_key,
            )
            self._payments_by_key[idempotency_key] = payment
            self._payments_by_id[payment.payment_id] = payment
            return payment

    def refund(self, payment_id: str) -> bool:
        with self._lock:
            payment = self._payments_by_id.get(payment_id)
            if payment is None:
                return False
            if payment.status == PaymentStatus.REFUNDED:
                return True
            if payment.status != PaymentStatus.SUCCESS:
                return False
            payment.status = PaymentStatus.REFUNDED
            return True

    def get(self, payment_id: str) -> Optional[Payment]:
        with self._lock:
            return self._payments_by_id.get(payment_id)


class TicketService:
    def __init__(self) -> None:
        self._tickets_by_booking: dict[str, list[Ticket]] = {}

    def issue(self, booking: Booking) -> list[Ticket]:
        existing = self._tickets_by_booking.get(booking.booking_id)
        if existing:
            return list(existing)
        tickets = [
            Ticket(
                ticket_id=str(uuid.uuid4()),
                booking_id=booking.booking_id,
                flight_instance_id=booking.flight_instance_id,
                passenger_id=assignment.passenger.passenger_id,
                seat_number=assignment.seat_number,
            )
            for assignment in booking.assignments
        ]
        self._tickets_by_booking[booking.booking_id] = tickets
        return list(tickets)

    def cancel_for_booking(self, booking_id: str) -> None:
        for ticket in self._tickets_by_booking.get(booking_id, []):
            ticket.status = TicketStatus.CANCELLED

    def get_for_booking(self, booking_id: str) -> list[Ticket]:
        return list(self._tickets_by_booking.get(booking_id, []))


class PriceService:
    def __init__(
        self,
        inventory: InventoryRepository,
        clock: Callable[[], datetime] = utc_now,
        quote_lifetime: timedelta = timedelta(minutes=5),
        currency: str = "USD",
    ) -> None:
        self._inventory = inventory
        self._clock = clock
        self._quote_lifetime = quote_lifetime
        self._currency = currency.upper()
        if len(self._currency) != 3 or not self._currency.isalpha():
            raise ValueError("currency must be a three-letter code")

    def create_quote(
        self, flight_instance_id: str, seat_numbers: Sequence[str]
    ) -> FareQuote:
        if not seat_numbers or len(set(seat_numbers)) != len(seat_numbers):
            raise ValueError("select one or more unique seats")
        seat_prices: list[tuple[str, Decimal]] = []
        for seat_number in seat_numbers:
            seat = self._inventory.get_seat(flight_instance_id, seat_number)
            if seat is None:
                raise SeatUnavailableError(f"seat {seat_number} does not exist")
            seat_prices.append((seat_number, seat.definition.price))
        now = self._clock()
        _require_aware(now)
        return FareQuote(
            quote_id=str(uuid.uuid4()),
            flight_instance_id=flight_instance_id,
            seat_prices=tuple(seat_prices),
            total=sum((price for _, price in seat_prices), Decimal("0.00")),
            created_at=now,
            expires_at=now + self._quote_lifetime,
            currency=self._currency,
        )


class FlightSearchService:
    def __init__(self, flights: FlightRepository, inventory: InventoryRepository) -> None:
        self._flights = flights
        self._inventory = inventory

    def search(
        self,
        origin_code: str,
        destination_code: str,
        departure_date: date,
        passenger_count: int = 1,
    ) -> list[FlightInstance]:
        if passenger_count < 1:
            raise ValueError("passenger_count must be at least one")
        return [
            flight
            for flight in self._flights.search(origin_code, destination_code, departure_date)
            if len(self._inventory.available_seats(flight.instance_id)) >= passenger_count
        ]


class NotificationService:
    def __init__(self) -> None:
        self.notifications: list[Notification] = []

    def send(self, event_type: str, booking: Booking, message: str) -> None:
        self.notifications.append(Notification(event_type, booking.booking_id, message))


class BookingService:
    def __init__(
        self,
        flights: FlightRepository,
        inventory: InventoryRepository,
        bookings: BookingRepository,
        pricing: PriceService,
        payments: PaymentService,
        tickets: TicketService,
        notifications: Optional[NotificationService] = None,
        clock: Callable[[], datetime] = utc_now,
        hold_duration: timedelta = timedelta(minutes=10),
    ) -> None:
        self._flights = flights
        self._inventory = inventory
        self._bookings = bookings
        self._pricing = pricing
        self._payments = payments
        self._tickets = tickets
        self._notifications = notifications or NotificationService()
        self._clock = clock
        self._hold_duration = hold_duration
        self._lock = RLock()

    def create_booking(
        self,
        user_id: str,
        flight_instance_id: str,
        assignments: Sequence[PassengerSeat],
        idempotency_key: Optional[str] = None,
    ) -> Booking:
        with self._lock:
            return self._create_booking(
                user_id, flight_instance_id, assignments, idempotency_key
            )

    def _create_booking(
        self,
        user_id: str,
        flight_instance_id: str,
        assignments: Sequence[PassengerSeat],
        idempotency_key: Optional[str],
    ) -> Booking:
        if not assignments:
            raise ValueError("at least one passenger and seat are required")
        assignments = tuple(assignments)
        seat_numbers = tuple(assignment.seat_number for assignment in assignments)
        passenger_ids = tuple(assignment.passenger.passenger_id for assignment in assignments)
        if len(set(seat_numbers)) != len(seat_numbers):
            raise ValueError("each passenger must have a different seat")
        if len(set(passenger_ids)) != len(passenger_ids):
            raise ValueError("passengers must be unique within a booking")

        fingerprint = (
            user_id,
            flight_instance_id,
            tuple(
                (assignment.passenger.passenger_id, assignment.seat_number)
                for assignment in assignments
            ),
        )
        idempotency_key = idempotency_key or str(uuid.uuid4())
        existing = self._bookings.get_by_idempotency_key(idempotency_key)
        if existing:
            if existing.request_fingerprint != fingerprint:
                raise ValueError("booking idempotency key was reused with different data")
            if existing.status in (BookingStatus.FAILED, BookingStatus.CANCELLATION_PENDING):
                raise BookingError("the original booking request did not complete", existing)
            return existing

        flight = self._flights.get(flight_instance_id)
        if flight is None or flight.status != FlightStatus.SCHEDULED:
            raise FlightNotFoundError("scheduled flight was not found")
        now = self._clock()
        _require_aware(now)
        if flight.departure_at <= now:
            raise BookingError("flight has already departed")

        quote = self._pricing.create_quote(flight_instance_id, seat_numbers)
        booking = Booking(
            booking_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            user_id=user_id,
            flight_instance_id=flight_instance_id,
            assignments=assignments,
            quote=quote,
            created_at=now,
        )
        self._bookings.save(booking)

        if not self._inventory.hold_seats(
            flight_instance_id, seat_numbers, booking.booking_id, self._hold_duration
        ):
            booking.transition(BookingStatus.FAILED)
            self._bookings.save(booking)
            raise SeatUnavailableError("one or more requested seats are unavailable", booking)

        booking.transition(BookingStatus.PAYMENT_PENDING)
        self._bookings.save(booking)
        payment = self._payments.charge(
            booking.booking_id,
            booking.amount,
            booking.quote.currency,
            booking.booking_id,
        )
        booking.payment_id = payment.payment_id
        if payment.status != PaymentStatus.SUCCESS:
            self._inventory.release_seats(
                flight_instance_id, seat_numbers, booking.booking_id
            )
            booking.transition(BookingStatus.FAILED)
            self._bookings.save(booking)
            self._notifications.send("BOOKING_FAILED", booking, "Payment was declined")
            raise PaymentFailedError("payment failed; held seats were released", booking)

        if not self._inventory.confirm_seats(
            flight_instance_id, seat_numbers, booking.booking_id
        ):
            self._payments.refund(payment.payment_id)
            self._inventory.release_seats(
                flight_instance_id, seat_numbers, booking.booking_id
            )
            booking.transition(BookingStatus.FAILED)
            self._bookings.save(booking)
            raise BookingError("seat hold expired before confirmation; payment refunded", booking)

        booking.transition(BookingStatus.CONFIRMED)
        self._bookings.save(booking)
        try:
            self._tickets.issue(booking)
        except Exception as error:
            self._payments.refund(payment.payment_id)
            self._inventory.release_seats(
                flight_instance_id, seat_numbers, booking.booking_id
            )
            booking.transition(BookingStatus.FAILED)
            self._bookings.save(booking)
            raise BookingError("ticket issuance failed; booking was compensated", booking) from error
        self._notifications.send("BOOKING_CONFIRMED", booking, "Booking and tickets confirmed")
        return booking

    def cancel_booking(self, booking_id: str) -> bool:
        with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None:
                return False
            if booking.status == BookingStatus.CANCELLED:
                return True
            if booking.status not in (
                BookingStatus.CONFIRMED,
                BookingStatus.CANCELLATION_PENDING,
            ):
                return False
            if not booking.payment_id or not self._payments.refund(booking.payment_id):
                return False

            seat_numbers = tuple(item.seat_number for item in booking.assignments)
            if not self._inventory.can_release_seats(
                booking.flight_instance_id, seat_numbers, booking.booking_id
            ):
                if booking.status == BookingStatus.CONFIRMED:
                    booking.transition(BookingStatus.CANCELLATION_PENDING)
                    self._bookings.save(booking)
                self._notifications.send(
                    "CANCELLATION_RECONCILIATION_REQUIRED",
                    booking,
                    "Refund succeeded but seat release needs reconciliation",
                )
                return False
            if not self._inventory.release_seats(
                booking.flight_instance_id, seat_numbers, booking.booking_id
            ):
                if booking.status == BookingStatus.CONFIRMED:
                    booking.transition(BookingStatus.CANCELLATION_PENDING)
                    self._bookings.save(booking)
                self._notifications.send(
                    "CANCELLATION_RECONCILIATION_REQUIRED",
                    booking,
                    "Refund succeeded but seat release needs reconciliation",
                )
                return False
            if booking.status == BookingStatus.CONFIRMED:
                booking.transition(BookingStatus.CANCELLED)
            else:
                booking.transition(BookingStatus.CANCELLED)
            self._tickets.cancel_for_booking(booking.booking_id)
            self._bookings.save(booking)
            self._notifications.send("BOOKING_CANCELLED", booking, "Booking cancelled and refunded")
            return True

    def get_tickets(self, booking_id: str) -> list[Ticket]:
        return self._tickets.get_for_booking(booking_id)


def build_demo() -> tuple[BookingService, FlightSearchService, FlightInstance]:
    origin = Airport(
        "SFO", "San Francisco International", "San Francisco", "US", "America/Los_Angeles"
    )
    destination = Airport(
        "JFK", "John F. Kennedy International", "New York", "US", "America/New_York"
    )
    flight = Flight("flight-1", "AC101", "Example Air")
    departure = utc_now() + timedelta(days=14)
    instance = FlightInstance(
        instance_id="AC101-2027-01-01",
        flight=flight,
        origin=origin,
        destination=destination,
        departure_at=departure,
        arrival_at=departure + timedelta(hours=5, minutes=30),
        aircraft_id="aircraft-1",
    )
    aircraft = Aircraft(
        "aircraft-1",
        "Example 737",
        (
            SeatDefinition("12A", "ECONOMY", Decimal("325.00")),
            SeatDefinition("12B", "ECONOMY", Decimal("325.00")),
        ),
    )
    flights = FlightRepository()
    flights.add(instance)
    inventory = InventoryRepository()
    inventory.add_aircraft(instance.instance_id, aircraft)
    bookings = BookingRepository()
    pricing = PriceService(inventory)
    payments = PaymentService()
    tickets = TicketService()
    booking_service = BookingService(
        flights, inventory, bookings, pricing, payments, tickets
    )
    return booking_service, FlightSearchService(flights, inventory), instance


def main() -> None:
    booking_service, search_service, flight = build_demo()
    matches = search_service.search(
        flight.origin.code,
        flight.destination.code,
        flight.departure_at.astimezone(ZoneInfo(flight.origin.time_zone)).date(),
    )
    passenger = Passenger("PAX-1", "Alex", "Morgan", "alex@example.com")
    booking = booking_service.create_booking(
        "USER-1",
        matches[0].instance_id,
        [PassengerSeat(passenger, "12A")],
        idempotency_key="demo-booking-1",
    )
    print(f"Confirmed booking {booking.booking_id}: {booking.amount}")
    for ticket in booking_service.get_tickets(booking.booking_id):
        print(f"Ticket {ticket.ticket_id}: seat {ticket.seat_number}")


if __name__ == "__main__":
    main()