# LLD & HLD Interview Preparation

A hands-on collection of system-design practice for software engineering interviews. This repository is where I work through Low-Level Design (LLD) problems, High-Level Design (HLD) problems, and the code and notes that help me explain my decisions clearly.

The goal is not just to collect answers. Each case study should make the requirements, design choices, trade-offs, and failure handling easy to understand and discuss.

## Current Case Study

### Flight Booking System (LLD)

A runnable Python design for searching direct flights and booking seats for one or more passengers. It focuses on the parts of booking that need careful reasoning: seat availability, temporary holds, payment outcomes, idempotent retries, ticket issuance, and cancellation.

- [Design notes](flight_booking_system_lld.md): requirements, domain model, APIs, state transitions, sequence flows, concurrency, persistence, and trade-offs.
- [Python implementation](flight_booking_system.py): domain objects, repositories, and application services using the standard library.
- [Tests](test_flight_booking_system.py): booking, cancellation, expiry, payment failure, retries, and concurrent seat holds.

The implementation uses in-memory repositories and a simulated payment service so the example is easy to run. It is an interview-practice project, not a production booking platform.

## Repository Layout

```text
.
├── README.md
├── flight_booking_system.py
├── flight_booking_system_lld.md
├── test_flight_booking_system.py
└── .gitignore
```

## Run Locally

Requirements: Python 3.9 or newer. The current examples use only the Python standard library.

Run the flight-booking demo:

```bash
python3 flight_booking_system.py
```

Run the tests:

```bash
python3 -m unittest -v test_flight_booking_system
```

## How I Approach a Design Problem

For each new problem, I aim to work through the same interview-friendly sequence:

1. **Clarify scope**: identify users, use cases, constraints, and assumptions.
2. **Set requirements**: separate functional behavior from reliability, scale, and security needs.
3. **Model the domain**: define entities, relationships, ownership, and lifecycle states.
4. **Define boundaries**: identify APIs, services, repositories, and external dependencies.
5. **Walk the critical flows**: explain the happy path, concurrency, retries, and failure recovery.
6. **Compare trade-offs**: explain what the design optimizes for and what would change at production scale.
7. **Verify the design**: use focused tests for important rules and edge cases.

For LLD, the emphasis is on object responsibilities, interfaces, state transitions, and testable behavior. For HLD, the emphasis is on system boundaries, data flow, storage, scaling, availability, and operational trade-offs.

## Practice Roadmap

This repository will grow as I complete and document more designs. Possible future exercises include:

- **LLD**: parking lot, library management, elevator control, and expense sharing.
- **HLD**: URL shortener, rate limiter, notification service, and news feed.

These are practice topics, not completed implementations. New entries will be added here as their designs and code are ready.

## Notes

- Designs are intentionally scoped for interview practice; assumptions are documented with each case study.
- Production concerns such as durable storage, real payment integrations, authentication, deployment, and monitoring are discussed where useful, but are not necessarily implemented.
- This is a personal learning repository. The designs are starting points for discussion, not universal or production-ready blueprints.
