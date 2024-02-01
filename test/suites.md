# Test suites

## Suite 1: Route 53 primitives only
- Domain "A"
  - Critical first
  - Critical second
  - All of them succeeds
- Expected: falls through to the next case

--

- Domain "A"
  - Same as last
  - Does nothing on this run
- Domain "B":
  - 3 Route 53 update
    - first: fails but non-critical
    - second: fails and it's critical
- Expected: Rolls back to the previous state (no records from domain "B")

<!-- --

(TODO)
- Domain "A" and "B"
- Route 53 update: critical A and AAAA
- Expected: succeeds
- Clean up all domains -->

## Suite 2: Volume primitives only
- Domain "A"
  - 1 volume: x
  - Run this twice. Should succeed

--

- Domain "A"
  - 1 volume: x
- Domain "B"
  - 3 volumes: pc
  - First one: fails but non-critical
  - Second one: succeeds
  - Last one: fails and critical
- Clean up domain "B"

--

- Domain "A"
  - 3 volumes: pc
- Run twice
  - First run creates all of the volumes
  - Second run does nothing
- Expected
  - Total of 3 volumes created and attached at the end of init
  - Falls through to the next case
- Clean up domain "A"
