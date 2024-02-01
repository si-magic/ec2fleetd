all: conf suite-1 suite-2

clean:
	./ec2fleetd-test.sh clean_volumes $(cat domains)
	./ec2fleetd-test.sh clean_rrs $(cat rrs)
	rm -rf suites/*/cache

suite-%:
	mkdir -p suites/$*/cache
	./ec2fleetd-test.sh test suites/$*
