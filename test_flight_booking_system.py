from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import threading
import unittest

from LLD_Practice.flight_booking_system import (
    Airport,
    Aircraft,
    BookingError,
    BookingRepository,
    BookingService,
    BookingStatus,
    Flight,
    FlightInstance,
    FlightNotFoundError,
    FlightRepository,
    FlightSearchService,
    InventoryRepository,
    Passenger,
    PassengerSeat,
    PaymentFailedError,
    PaymentService,
    PaymentStatus,
    PriceService,
    SeatDefinition,
    SeatStatus,
    SeatUnavailableError,
    TicketService,
    TicketStatus,
    utc_now,
)


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


class FailingTicketService(TicketService):
    def issue(self, booking):
        raise RuntimeError("ticket provider unavailable")


class FlightBookingSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
        self.origin = Airport(
            "SFO", "San Francisco International", "San Francisco", "US", "America/Los_Angeles"
        )
        self.destination = Airport(
            "JFK", "John F. Kennedy International", "New York", "US", "America/New_York"
        )
        self.flight = Flight("flight-1", "AC101", "Example Air")
        self.instance = FlightInstance(
            instance_id="instance-1",
            flight=self.flight,
            origin=self.origin,
            destination=self.destination,
            departure_at=datetime(2027, 1, 2, 2, tzinfo=timezone.utc),
            arrival_at=datetime(2027, 1, 2, 7, tzinfo=timezone.utc),
            aircraft_id="aircraft-1",
        )
        self.flights = FlightRepository()
        self.flights.add(self.instance)
        self.inventory = InventoryRepository(clock=self.clock)
        self.inventory.add_aircraft(
            self.instance.instance_id,
            Aircraft(
                "aircraft-1",
                "Example 737",
                (
                    SeatDefinition("12A", "ECONOMY", Decimal("325.00")),
                    SeatDefinition("12B", "ECONOMY", Decimal("350.00")),
                ),
            ),
        )
        self.bookings = BookingRepository()
        self.pricing = PriceService(self.inventory, clock=self.clock)
        self.payments = PaymentService()
        self.tickets = TicketService()
        self.service = self.make_service()
        self.search = FlightSearchService(self.flights, self.inventory)

    def make_service(
        self,
        payments=None,
        tickets=None,
        hold_duration=timedelta(minutes=10),
    ):
        return BookingService(
            self.flights,
            self.inventory,
            self.bookings,
            self.pricing,
            payments or self.payments,
            tickets or self.tickets,
            clock=self.clock,
            hold_duration=hold_duration,
        )

    @staticmethod
    def assignments(*seat_numbers):
        return [
            PassengerSeat(
                Passenger(f"pax-{index}", f"First{index}", "Passenger", f"p{index}@example.com"),
                seat_number,
            )
            for index, seat_number in enumerate(seat_numbers, start=1)
        ]

    def test_search_filters_by_origin_local_date_and_capacity(self):
        local_date = date(2027, 1, 1)
        self.assertEqual(
            [self.instance],
            self.search.search("sfo", "jfk", local_date, passenger_count=2),
        )
        self.assertEqual([], self.search.search("SFO", "JFK", date(2027, 1, 2)))
        self.assertEqual([], self.search.search("SFO", "LAX", local_date))

    def test_booking_confirms_multiple_seats_and_issues_tickets(self):
        booking = self.service.create_booking(
            "user-1", self.instance.instance_id, self.assignments("12A", "12B")
        )

        self.assertEqual(BookingStatus.CONFIRMED, booking.status)
        self.assertEqual(Decimal("675.00"), booking.amount)
        self.assertEqual("USD", booking.quote.currency)
        self.assertEqual(2, len(self.service.get_tickets(booking.booking_id)))
        self.assertTrue(
            all(
                self.inventory.get_seat(self.instance.instance_id, seat).status
                == SeatStatus.CONFIRMED
                for seat in ("12A", "12B")
            )
        )

    def test_concurrent_holds_allow_only_one_winner(self):
        barrier = threading.Barrier(2)

        def hold(booking_id):
            barrier.wait()
            return self.inventory.hold_seats(
                self.instance.instance_id,
                ["12A"],
                booking_id,
                timedelta(minutes=5),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(hold, ("booking-a", "booking-b")))

        self.assertEqual(1, sum(results))

    def test_unavailable_seat_fails_without_charging(self):
        first = self.service.create_booking(
            "user-1", self.instance.instance_id, self.assignments("12A")
        )
        with self.assertRaises(SeatUnavailableError) as raised:
            self.service.create_booking(
                "user-2", self.instance.instance_id, self.assignments("12A")
            )

        self.assertEqual(BookingStatus.CONFIRMED, first.status)
        self.assertEqual(BookingStatus.FAILED, raised.exception.booking.status)

    def test_payment_failure_releases_all_held_seats(self):
        failed_payments = PaymentService(charge_succeeds=lambda _booking, _amount: False)
        service = self.make_service(payments=failed_payments)

        with self.assertRaises(PaymentFailedError) as raised:
            service.create_booking(
                "user-1", self.instance.instance_id, self.assignments("12A", "12B")
            )

        booking = raised.exception.booking
        self.assertEqual(BookingStatus.FAILED, booking.status)
        self.assertEqual(
            PaymentStatus.FAILED,
            failed_payments.get(booking.payment_id).status,
        )
        self.assertTrue(
            all(
                self.inventory.get_seat(self.instance.instance_id, seat).status
                == SeatStatus.AVAILABLE
                for seat in ("12A", "12B")
            )
        )

    def test_expired_hold_can_be_reacquired_but_not_confirmed_by_old_owner(self):
        self.assertTrue(
            self.inventory.hold_seats(
                self.instance.instance_id, ["12A"], "old-booking", timedelta(minutes=1)
            )
        )
        self.clock.advance(timedelta(minutes=2))

        self.assertTrue(
            self.inventory.hold_seats(
                self.instance.instance_id, ["12A"], "new-booking", timedelta(minutes=1)
            )
        )
        self.assertFalse(
            self.inventory.confirm_seats(
                self.instance.instance_id, ["12A"], "old-booking"
            )
        )
        self.assertEqual(
            "new-booking",
            self.inventory.get_seat(self.instance.instance_id, "12A").reserved_by,
        )

    def test_expiry_during_payment_refunds_payment_and_releases_seats(self):
        def charge_then_advance(_booking, _amount):
            self.clock.advance(timedelta(minutes=2))
            return True

        payments = PaymentService(charge_succeeds=charge_then_advance)
        service = self.make_service(
            payments=payments,
            hold_duration=timedelta(minutes=1),
        )

        with self.assertRaises(BookingError) as raised:
            service.create_booking(
                "user-1", self.instance.instance_id, self.assignments("12A")
            )

        booking = raised.exception.booking
        self.assertEqual(BookingStatus.FAILED, booking.status)
        self.assertEqual(PaymentStatus.REFUNDED, payments.get(booking.payment_id).status)
        self.assertEqual(
            SeatStatus.AVAILABLE,
            self.inventory.get_seat(self.instance.instance_id, "12A").status,
        )

    def test_cancellation_refunds_releases_seats_and_is_idempotent(self):
        booking = self.service.create_booking(
            "user-1", self.instance.instance_id, self.assignments("12A")
        )

        self.assertTrue(self.service.cancel_booking(booking.booking_id))
        self.assertTrue(self.service.cancel_booking(booking.booking_id))
        self.assertEqual(BookingStatus.CANCELLED, booking.status)
        self.assertEqual(PaymentStatus.REFUNDED, self.payments.get(booking.payment_id).status)
        self.assertEqual(
            SeatStatus.AVAILABLE,
            self.inventory.get_seat(self.instance.instance_id, "12A").status,
        )
        self.assertEqual(TicketStatus.CANCELLED, self.service.get_tickets(booking.booking_id)[0].status)

        replacement = self.service.create_booking(
            "user-2", self.instance.instance_id, self.assignments("12A")
        )
        self.assertEqual(BookingStatus.CONFIRMED, replacement.status)

    def test_booking_retry_with_same_key_returns_existing_booking(self):
        charges = []
        payments = PaymentService(
            charge_succeeds=lambda booking, amount: charges.append((booking, amount)) or True
        )
        service = self.make_service(payments=payments)
        assignments = self.assignments("12A")
        first = service.create_booking(
            "user-1", self.instance.instance_id, assignments, "request-1"
        )
        retry = service.create_booking(
            "user-1", self.instance.instance_id, assignments, "request-1"
        )

        self.assertIs(first, retry)
        self.assertEqual(1, len(charges))
        self.assertEqual(1, len(service.get_tickets(first.booking_id)))

    def test_reused_booking_key_with_different_request_is_rejected(self):
        self.service.create_booking(
            "user-1", self.instance.instance_id, self.assignments("12A"), "request-1"
        )

        with self.assertRaises(ValueError):
            self.service.create_booking(
                "user-1", self.instance.instance_id, self.assignments("12B"), "request-1"
            )

    def test_ticket_failure_compensates_payment_and_inventory(self):
        service = self.make_service(tickets=FailingTicketService())

        with self.assertRaises(BookingError) as raised:
            service.create_booking(
                "user-1", self.instance.instance_id, self.assignments("12A")
            )

        booking = raised.exception.booking
        self.assertEqual(BookingStatus.FAILED, booking.status)
        self.assertEqual(PaymentStatus.REFUNDED, self.payments.get(booking.payment_id).status)
        self.assertEqual(
            SeatStatus.AVAILABLE,
            self.inventory.get_seat(self.instance.instance_id, "12A").status,
        )

    def test_unknown_flight_is_rejected(self):
        with self.assertRaises(FlightNotFoundError):
            self.service.create_booking(
                "user-1", "missing-flight", self.assignments("12A")
            )


if __name__ == "__main__":
    unittest.main()
