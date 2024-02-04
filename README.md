# EC2fleetd
<img src="assets/drawing.export.svg" alt="EC2fleetd icon" width="128"/>

EC2fleetd is the home-brewed EC2 instance resource set up automation daemon. Its
goal is to to make it easier to run the applications that do not cope well in
the spot fleet.

(c) 2024 David Timber &lt;dxdt 𝕒𝕥 dev.snart.me&gt;

EC2fleetd is made to address the following issues:

- Maintaining the persistent application data by having "volume pools"
  - Persisting distributed scientific computing(BOINC) data
  - Making home-grade game servers tolerant to spot interruptions (ArmA,
    Minecraft, Rust ...)[^1]
  - HA'ing cryptocurrency nodes
- DNS record updates for single-instance fleets for monolithic applications such
  as MTA and game servers

The usual flow would be

1. An instance is launched in the fleet
1. The instance boots up and starts EC2fleetd
1. EC2fleetd attaches an existing volume containing application data already
   populated by the previous instance. Or create a volume from the snapshot if
   required
1. EC2fleetd starts the application
1. The application starts servicing off the data in the volume

Upon interruption,

1. EC2fleetd polls interruption warning notice from the IMDS
1. In the even of valid interruption warning notice, EC2fleetd runs the
   configured commands. The commands should usually do ...
    - Warn users/players on the server
    - Save data and stop the application
    - Unmount the volume
1. EC2fleetd notifies and exits. The instance is left running
1. At the time of termination, the instance gets shut down and terminated

EC2fleetd also offers notifications via SNS and SQS. The contents of the
notifications can be set so that it's useful in troubleshooting when something
goes wrong. This cannot be done using CloudWatch alone.

## What you have to do on top of this
### Custom scripts for your use case
Or multiple Exec lines could do the trick!

### Volume Pool
- Provisioning the volume pool according to the capacity of the fleet
- Maintain versions of data volume snapshots to accelerate the spin up process
  - For Bitcoin nodes, it just makes sense to renew the snapshot every now and
    then because the blockchain data grows incrementally by design
- (the "c" volume source)Make your own manager app that periodically reclaims
  old volumes if necessary

## INSTALL
Make/modify the IAM role for the instance. See [doc/perms.md](doc/perms.md) for
required permissions.

Advanced: use resource groups to further isolate the scope of EC2fleetd's
operation[^3].

### In the fleet image
Install the systemd units in [systemd](systemd). Install the Python module.

```sh
python -m pip install --upgrade ec2fleetd
```

### The config
There are two options.

1. Specify the config through the user data of the launch template
1. Ship it with the image

Without `--userdata` option, EC2fleetd treats the user data as the daemon
config. This will give you more flexibility when the image is set up for more
than one application. To use that way, enable the non-instanced Systemd
service `ec2fleetd.service`.

To ship the config files with the image, place the config files in
`/etc/ec2fleetd` and enable the instanced Systemd service `ec2fleetd@.service`.
For example, to enable `/etc/ec2fleetd/my-app.jsonc`, run `systemctl enable
ec2fleetd@my-app.service`.

## Config Examples
### Game Server
A Minecraft server on spot would look like this.

```jsonc
{
  "$schema": "http://schema.si-magic.com/fleet-init/json/userdata/0",

  "hostname": "my-world.mc",

  "domains": {
    "my-world.mc": {
      "attach-volume": [
        {
          "device": "/dev/xvdf",
          "source": "x",
          "volume-id": "vol-NNNNNNNNNNNNNNNNN",
          "exec": [
            {
              "lines": [
                {
                  "argv": [ "/bin/mount", "/mnt/mc" ]
                }
              ]
            }
          ]
        }
      ],

      "update-route53": [
        {
          "hostedzone": "ZZZZZZZZZZZZZZZZZZZZZ",
          "name": "my-world.mc",
          "ttl": 300
        }
      ],

      "exec": [
        {
          "on": [ "started" ],
          "lines": [
            { "argv": [ "/bin/systemctl", "start", "minecraft.service" ] }
          ]
        },
        {
          "on": [ "interrupted" ],
          "lines": [
            /*
             * A script would be used to warn the users and stop
             * the server after a certain amount of time.
             */
            { "argv": [ "/path/to/interrupt/script" ] }
          ]
        }
      ],

      "notify": [
        {
          "backend": "aws-sns", // backend module name
          "options": { // backend-specific data to be passed to the module
            "region": "REGION",
            "topic": "TOPIC_ARN"
          }
        }
      ]
    }
  }
}
```

### Crypto
Couple of Bitcoin nodes.

```jsonc
{
  "$schema": "http://schema.si-magic.com/fleet-init/json/userdata/0",

  "hostname": "node-{ami-i}.btcn.example.com",

  "domains": {
    "btcn.example.com": {
      "attach-volume": [
        {
          "device": "/dev/xvdf",
          "source": "p",
          "pool-name": "btc-nodes",
          "exec": [
            {
              "lines": [
                {
                  "argv": [ "/bin/mount", "/mnt/btc" ]
                }
              ]
            }
          ]
        }
      ],

      // Naturally, there should be a CNAME RR pointing to these.
      "update-route53": [
        {
          "hostedzone": "ZZZZZZZZZZZZZZZZZZZZZ",
          "name": "node-{ami-i}.btcn.example.com",
          "ttl": 300
        }
      ],

      "exec": [
        {
          "on": [ "started" ],
          "lines": [
            { "argv": [ "/bin/systemctl", "start", "bitcoin.service" ] }
          ]
        },
        {
          "on": [ "interrupted" ],
          "lines": [
            { "argv": [ "/bin/systemctl", "stop", "bitcoin.service" ] },
            { "argv": [ "/bin/umount", "-l", "/mnt/btc" ] }
          ]
        }
      ],

      "notify": [
        {
          "backend": "aws-sns", // backend module name
          "options": { // backend-specific data to be passed to the module
            "region": "REGION",
            "topic": "TOPIC_ARN"
          }
        }
      ]
    }
  }
}
```

## Disclaimer
This project is GPL'd.

> For the developers' and authors' protection, the GPL clearly explains
that there is no warranty for this free software.

In other words, it means that the developer(s) of EC2fleetd cannot be held
responsible for any cost incurred from misuse or software error.

## Quirks
### The "c" attach-volume source
The "c" volume source is a bad idea in large scale. In the ideal world, the
volume pool has to be provisioned by the sysadmin so the cost from volume pools
is somewhat predictable. Imagine a situation where all 100 instances in the
fleet are creating a volume every time they get launched because you forgot to
add "p".

### update-route53
When EC2fleetd exist, it does not delete the RR's it has created during the init
process. This is intentional because `update-route53` is made as a cheap hack.
The problem where the clients end up in the wrong house exists(I don't know the
technical term for this. Sorry). For improved security and reliability, consider
following options.

- Elastic Beanstalk in conjunction with Route 53(BEST!)
- Use load balancers in conjunction with Route 53
- TLS stack
  - Do server verification using CA
  - SNI
- Virtual host: HTTP/1.1 "Host" header, SMTP HELO or the likes
- HMAC
  - OpenVPN tls-ta

## TODOs
### Multi-domain inits are not run in parallel
AWS requests are painfully slow, with round-trip times range from 100's ms.
Nothing can be done for this, but at least we can alleviate the problem by using
AWS API in parallel.

This was considered in the design phase, but had to release EC2fleetd without
parallelism due to the limitations of Python's concurrency facilities. It's
totally possible to make EC2fleetd this way. It's just that doing inter-thread
or inter-process signalling with Python is pain in the bum. I might as well have
written EC2fleetd in C.

### What about Azure and Google?
Other CSP's do offer spot instances. I started writing code with the fact in
mind, but had to take some shortcuts because I was running out of time.
Obviously, other CSP's have different approaches when it comes to resource
addressing, types, lifecycle and so on... so much more research is needed to
make an abstraction layer that can accommodate the big players in cloud
computing.

### JSON Schema
The schema url is only a placeholder at the moment. There is definitely a need
for one here in the grand scheme of things. See [Quirks](#Quirks) for more.

### LRU and MRU[^2]
The current EC2fleetd implementation picks one volume from the pool based on the
instance index. Some use cases may benefit from LRU and MRU policies.

### Unit tests
If I get paid to finish it, maybe.

## Notes
### All of this can be done using a user data script
For single-instance fleets? Yeah. But EC2fleetd could still save you some time
on Route 53 updates. Also, updating DNS records directly can save you some money
on ELB.

## EC2fleetd is reentrant
Meaning that you can stop and start EC2fleetd service once it successfully sets
up the configured domains. The second run should make no changes to any AWS
resources(well, except for Route 53 RR's). You should make your exec scripts
reentrant as well, or specify event attribute or use macros.

Most Linux commands are designed in this way anyways.

- `mount`: most drivers do nothing if already mounted
- `systemctl start|stop`: do nothing if the unit is already started/stopped

[^1]: https://www.battlemetrics.com/servers
[^2]: https://en.wikipedia.org/wiki/Cache_replacement_policies
[^3]: https://docs.aws.amazon.com/ARG/latest/userguide/security_iam_service-with-iam.html
