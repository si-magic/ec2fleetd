# Things to test

- TransientResourceManager(True)
- TransientResourceManager(False)

## Transaction Rollback
- EC2CreatedVolumeHold
- EC2AttachedVolumeHold
- Route53InsertedRRHold
- Route53UpdatedRRHold
- AWSResourceTranscLog

### with ...
- Multiple domains?

## Independently
- hostname
- EC2MetaManager
  - InterruptSchedule
- Notify Backends
  - SNSNotifyBackend
  - SQSNotifyBackend

## Test Facilities

- test domain cleaner
  - Capable of ...
    - Detach test volumes
    - Delete test volumes
    - Delete test route 53 RRs
	- Delete state files created by test exec scripts
  - Using ...
    - Domain tags
	<!-- - Transaction tags -->
<!-- - Test set up
  - AWS profile --> Use EC2 Role
