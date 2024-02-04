import getopt
import glob
import io
import random
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import boto3
import botocore
import pyjson5
import sdnotify

import ec2fleetd
from ec2fleetd import *
from ec2fleetd import aws
from ec2fleetd.exceptions import *

assert(threading.current_thread() == threading.main_thread())

stdout_lock = threading.Lock()

class EC:
	OK = 0
	GENERIC_ERR = 1
	USAGE_ERR = 2

class RunParam:
	'''Params from command line'''

	def __init__ (self):
		self.help: bool = False
		self.version: bool = False
		self.verbose: int = 0
		self.imds: str = None
		self.userdata: str = None
		self.transc_id = str(uuid.uuid4())
		self.profile = None
		self.enable_init = True
		self.enable_notify = True
		self.enable_exec = True
		self.enable_poll = True

	def disable_all (self):
		self.enable_init = False
		self.enable_notify = False
		self.enable_exec = False
		self.enable_poll = False

def print_ver (out: io.TextIOBase) -> int:
	return out.write(
'''Version: {v}
'''.format(v = ec2fleetd.ver))

def print_help (out: io.TextIOBase, program: str) -> int:
	return out.write(
'''EC2 fleet init daemon
Usage: ec2fleetd [options]
Options:
  --help, -h              print this message and exit
  --imds=<HOST>           override the IMDS endpoint
  --userdata=<FILE>       read user data from the file instead of fetching it
                          from the IMDS endpoint
  --transc_id=<STR>       set the transaction id to the given value
  --profile=<STR>         set the Boto3 profile (for debugging only!)
  -v                      reserved (ignored)
  -V                      print version and exit
  --disable-all           disable all features
  --enable-init=<BOOL>    enable init parts (volumes, route 53, hostname)
  --enable-notify=<BOOL>  enable notify directives
  --enable-exec=<BOOL>    enable exec directives
  --enable-poll=<BOOL>    enable polling of interruption notice
'''.format(program = program))

def parse_argv (argv: list) -> RunParam:
	ret = RunParam()

	opts, args = getopt.getopt(
		argv,
		"vVh",
		[
			"help",
			"imds=",
			"userdata=",
			"transc_id=",
			"profile=",
			"disable-all",
			"enable-init=",
			"enable-notify=",
			"enable-exec=",
			"enable-poll="
		])
	for opt in opts:
		match opt[0]:
			case "-h" | "--help": ret.help = True
			case "-v": ret.verbose += 1
			case "-V": ret.version = True
			case "--imds": ret.imds = opt[1]
			case "--userdata": ret.userdata = opt[1]
			case "--transc_id":
				ret.transc_id = opt[1]
				if not ret.transc_id:
					raise ValueError(ret.transc_id + ": invalid --transc_id option")
			case "--profile":
				ret.profile = opt[1]
			case "disable-all":
				ret.disable_all()
			case "--enable-init":
				ret.enable_init = ec2fleetd.parse_bool(opt[1])
			case "--enable-notify":
				ret.enable_notify = ec2fleetd.parse_bool(opt[1])
			case "--enable-exec":
				ret.enable_exec = ec2fleetd.parse_bool(opt[1])
			case "--enable-poll":
				ret.enable_poll = ec2fleetd.parse_bool(opt[1])

	return ret

# parse options
try:
	run_param = parse_argv(sys.argv[1:])
	mm = aws.EC2MetaManager(run_param.imds)
except ValueError as e:
	with stdout_lock:
		ec2fleetd.pexcept(e)
	exit(EC.USAGE_ERR)

def open_userdata () -> io.BufferedIOBase:
	if run_param.userdata:
		return open(run_param.userdata, "r")
	return mm.open_userdata()

# do print options
if run_param.version:
	print_ver(sys.stdout)
if run_param.help:
	print_help(sys.stdout, sys.argv[0])

if run_param.version or run_param.help:
	exit(EC.OK)

sdn = sdnotify.SystemdNotifier()

# crawl data
ms = MacroSet()
ms.daemon_state = DaemonState.STARTING
ms.transaction_id = run_param.transc_id
mm.fetch_meta(ms)

# warn unsupported hypervisor system
if not magic.is_supported_hv(ms.hypervisor):
	sys.stderr.write(
		'''Unsupported hypervisor: {hv}{nl}'''.format(
			hv = ms.hypervisor,
			nl = os.linesep)
	)

with open_userdata() as f:
	try:
		fleet_conf = pyjson5.load(f)
	except pyjson5.Json5EOF:
		if f.tell() == 0:
			sys.stderr.write("Empty user data. Bye bye!" + os.linesep)
			exit(EC.OK)
		raise

'''
Start from cheap to expensive in terms of monetary cost. Run them in a child so
that you could time it. Do in this order.

- domain init
  - update-route53
  - attach-network-interface (planned?)
  - attach-volume
- set-hostname
- exec
- notify
'''

def redirsig_main (sn) -> bool:
	th_main = threading.main_thread()
	th_cur = threading.current_thread()

	if th_main != th_cur:
		signal.pthread_kill(th_main.ident, sn)
		return True

	return False

def handle_interrupt (sn, *args):
	'''Direct the signal to the main thread. Raise InterruptedError in the main
	thread.'''
	if redirsig_main(sn):
		return

	# Subsequent signals will kill the process
	signal.signal(sn, signal.SIG_DFL)

	raise InterruptedError("Interrupted by signal #" + str(sn))

def handle_timeout (sn, *args):
	raise TimeoutError()

signal.signal(signal.SIGINT, handle_interrupt)
signal.signal(signal.SIGTERM, handle_interrupt)
signal.signal(signal.SIGALRM, handle_timeout)

def mk_dexecutor (max_workers = None):
	if max_workers is None:
		max_workers = len(fleet_conf["domains"].keys())

	return ThreadPoolExecutor(max_workers)

def fs_wait_all (fs: list[futures.Future], timeout: float = None):
	futures.wait(
		# timeout = timeout, # TODO
		fs = fs,
		return_when = futures.ALL_COMPLETED)

def fs_cancel_all (fs: list[futures.Future]):
	for f in fs:
		f.cancel()

def filter_transient_vols (
		it: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
	ret = list[dict[str, Any]]()

	for vol in it:
		found = False
		for tag in vol.get("Tags", []):
			if tag.get("Key") == aws.Magic.TagName.TRANSC_ID:
				found = True
				break

		if not found and vol["State"] == "available":
			ret.append(vol)

	return ret

def wait_for_path (path: str) -> str:
	if glob.glob(path):
		return path
	return

def do_volume (
		conf: dict[str, Any],
		ms: MacroSet,
		t_parent: TransientResourceManager,
		t_logger: ResourceTransactionLogger,
		client: aws.BotoClientWrapper):
	dev_path = conf["device"]
	src_p = conf["source"]
	vid = conf.get("volume-id")
	pname = conf.get("pool-name")
	create_param = conf.get("create")
	rng = random.Random()

	def src_vol_x (transc: TransientResourceManager) -> int:
		rsp = client.do_call(
			"describe_volumes",
			t_logger,
			Filters = [
				{
					"Name": "attachment.instance-id",
					"Values": [ ms.instance_id ]
				},
				{
					"Name": "attachment.status",
					"Values": [ "attached" ]
				},
				{
					"Name": "volume-id",
					"Values": [ vid ]
				}
			])
		vol = rsp["Volumes"]

		assert len(vol) <= 1
		if len(vol):
			vol = vol[0]
			for att in vol["Attachments"]:
				if att["InstanceId"] == ms.instance_id:
					att_dev = att["Device"]
					if att_dev != dev_path:
						raise VolumeAttachedError(
							'''{vid} attached as {att_dev}'''
							.format(att_dev = att_dev, vid = vid))
					return 0

		try:
			client.do_call(
				"attach_volume",
				t_logger,
				Device = dev_path,
				InstanceId = ms.instance_id,
				VolumeId = vid)
			transc.push([ aws.EC2AttachedVolumeHold(
				client,
				vid,
				ms.transaction_id,
				t_logger) ])

			client.do_call(
				"create_tags",
				t_logger,
				Resources = [ vid ],
				Tags = [{
					"Key": aws.Magic.TagName.TRANSC_ID,
					"Value": ms.transaction_id }])
		except botocore.exceptions.ClientError:
			return -1
		else:
			return 1

	def src_vol_p (transc: TransientResourceManager) -> int:
		nonlocal vid
		run_cnt = 0

		rsp = client.do_call(
			"describe_volumes",
			t_logger,
			Filters = [
				{
					"Name": "tag:" + aws.Magic.TagName.DOMAIN,
					"Values": [ ms.domain ]
				},
				{
					"Name": "tag:" + aws.Magic.TagName.POOL_NAME,
					"Values": [ pname ]
				},
				{
					"Name": "attachment.instance-id",
					"Values": [ ms.instance_id ]
				},
				{
					"Name": "attachment.status",
					"Values": [ "attached" ]
				}
			])

		for vol in rsp["Volumes"]:
			for att in vol["Attachments"]:
				if att["InstanceId"] == ms.instance_id:
					att_dev = att["Device"]
					if att_dev == dev_path:
						vid = vol["VolumeId"]
						return 0
					else:
						raise VolumeAttachedError(
							'''{vid} from pool {pname} attached as {att_dev}'''
							.format(
								vid = vol["VolumeId"],
								pname = pname,
								att_dev = att_dev))

		while True:
			rsp = client.do_call(
				"describe_volumes",
				t_logger,
				Filters = [
					{
						"Name": "tag:" + aws.Magic.TagName.DOMAIN,
						"Values": [ ms.domain ]
					},
					{
						"Name": "tag:" + aws.Magic.TagName.POOL_NAME,
						"Values": [ pname ]
					},
					{
						"Name": "availability-zone",
						"Values": [ ms.placement_zone ]
					}
				])

			if len(rsp["Volumes"]) == 0:
				return -1

			def pick_vol ():
				'''
				Depends on the assumption that the order of volumes in the list are
				somewhat persistent. Otherwise, the response should be sorted using
				our own criteria.
				'''
				if run_cnt == 0 and ms.instance_index is not None:
					vols = rsp["Volumes"]
					vol_len = len(vols)

					ret = vols[ms.instance_index % vol_len]
					if ret["State"] == "available":
						return ret["VolumeId"]

				vols = filter_transient_vols(rsp["Volumes"])
				vol_len = len(vols)

				if False and run_cnt != 0: # FIXME
					# this case will cause thundering herd. Needs to be fixed.
					with stdout_lock:
						sys.stderr.write(
							"No instance index. Picking random volume from pool")

				if vol_len == 0:
					return None

				return vols[rng.randint(0, vol_len - 1)]["VolumeId"]

			vid = pick_vol()
			if not vid:
				return -1

			try:
				client.do_call(
					"attach_volume",
					t_logger,
					Device = dev_path,
					InstanceId = ms.instance_id,
					VolumeId = vid)
				transc.push([ aws.EC2AttachedVolumeHold(
					client,
					vid,
					ms.transaction_id,
					t_logger) ])

				client.do_call(
					"create_tags",
					t_logger,
					Resources = [ vid ],
					Tags = [{
						"Key": aws.Magic.TagName.TRANSC_ID,
						"Value": ms.transaction_id
					}])

				return 1
			except botocore.exceptions.ClientError:
				run_cnt += 1

	def src_vol_c (transc: TransientResourceManager) -> int:
		nonlocal vid

		extra_tags = [
			{
				"Key": aws.Magic.TagName.DOMAIN,
				"Value": ms.domain
			},
			{
				"Key": aws.Magic.TagName.POOL_NAME,
				"Value": pname
			},
			{
				"Key": aws.Magic.TagName.TRANSC_ID,
				"Value": ms.transaction_id
			},
			{
				"Key": aws.Magic.TagName.IN_TRANSIT,
				"Value": "true"
			},
		]
		tag_spec = create_param.get("TagSpecifications", [])
		aws.add_extra_tags(extra_tags, "volume", tag_spec)
		create_param["TagSpecifications"] = tag_spec

		create_param["AvailabilityZone"] = ms.placement_zone

		rsp = client.do_call("create_volume", t_logger, **create_param)
		vid = rsp["VolumeId"]
		state = rsp["State"]
		transc.push([ aws.EC2CreatedVolumeHold(client, vid, t_logger) ])

		wait_steps = aws.EC2VolumeCreatePollWaitStep()
		while state == "creating":
			wait_time = wait_steps.next()
			time.sleep(wait_time)

			rsp = client.do_call(
				"describe_volumes",
				t_logger,
				VolumeIds = [ vid ])

			state = rsp["Volumes"][0]["State"]

		client.do_call(
			"attach_volume",
			t_logger,
			Device = dev_path,
			InstanceId = ms.instance_id,
			VolumeId = vid)
		transc.push([ aws.EC2AttachedVolumeHold(
			client,
			vid,
			ms.transaction_id,
			t_logger) ])

		return 1

	local_ms = deepcopy(ms)
	with TransientResourceManager(conf.get("critical", True), t_parent) as transc:
		rv = -1
		for src in src_p:
			match src:
				case 'x':
					rv = src_vol_x(transc)
				case 'p':
					rv = src_vol_p(transc)
				case 'c':
					rv = src_vol_c(transc)
				case _:
					raise ValueError(src + ": invalid source spec")

			if rv >= 0:
				local_ms.attach_source = src
				break

		if rv < 0:
			raise NoVolumeSourceError(dev_path + ": no source available")
		elif rv > 0:
			local_ms.attach_op = "true"
		else:
			local_ms.attach_op = "false"
		local_ms.volume_id = vid
		local_ms.volume_pool = pname

		assert vid
		# Wait for the device to come up
		# FIXME: rpc udev rather than spin wait
		while True:
			local_ms.attached_device = (
				aws.find_blockdev_by_vid(vid) or
				wait_for_path(dev_path))
			if local_ms.attached_device:
				break
			time.sleep(magic.Code.DEVICE_WAIT)

		exec_mat = init_exec_mat(conf.get("exec", []), local_ms.format)
		do_exec_mat(exec_mat)

def do_route53 (
		conf: dict[str, Any],
		ms: MacroSet,
		t_parent: TransientResourceManager,
		t_logger: ResourceTransactionLogger,
		client: aws.BotoClientWrapper):
	hz = conf["hostedzone"]
	rname = conf["name"]
	rttl = conf["ttl"]
	rrs = []

	if ms.primary_public_ipv4:
		rrs.append({
			"Name": rname,
			"Type": "A",
			"TTL": rttl,
			"ResourceRecords": [
				{ "Value": s.strip() for s in ms.primary_public_ipv4.split(',') }
			]
		})
	if ms.primary_public_ipv6:
		rrs.append({
			"Name": rname,
			"Type": "AAAA",
			"TTL": rttl,
			"ResourceRecords": [
				{ "Value": s.strip() for s in ms.primary_public_ipv6.split(',') }
			]
		})

	if not rrs:
		return

	with TransientResourceManager(conf.get("critical", True), t_parent) as transc:
		rsp = client.do_call(
			"list_resource_record_sets",
			t_logger,
			HostedZoneId = hz,
			StartRecordName = rname)
		saved = []
		for rr in rsp["ResourceRecordSets"]:
			if rr["Name"] != rname:
				break
			saved.append(rr)

		batch = { "Changes": aws.mk_r53_rrchanges("UPSERT", rrs) }
		rsp = client.do_call(
			"change_resource_record_sets",
			t_logger,
			HostedZoneId = hz,
			ChangeBatch = batch)["ChangeInfo"]
		change_id = rsp["Id"]

		# wait for sync???
		# while rsp["Status"] == "PENDING":
		# 	time.sleep(1)
		# 	rsp = client.do_call("get_change", t_logger, Id = change_id)["ChangeInfo"]

		if saved:
			transc.push([ aws.Route53UpdatedRRHold(client, hz, saved, t_logger) ])
		else:
			transc.push([ aws.Route53InsertedRRHold(client, hz, rrs, t_logger) ])

def mk_profile ():
	kwargs = dict[str, Any]()
	if run_param.profile:
		kwargs["profile_name"] = run_param.profile
	kwargs["region_name"] = ms.placement_region

	return boto3.session.Session(**kwargs)

def do_domain_init (dname: str, conf: dict[str, Any]):
	local_ms = deepcopy(ms)
	local_ms.domain = dname

	session = mk_profile()
	c_ec2 = session.client("ec2")
	c_r53 = session.client("route53")

	t_logger = ResourceTransactionLogger()

	try:
		with TransientResourceManager(True) as transc:
			for vol_spec in conf.get("attach-volume", []):
				do_volume(
					vol_spec,
					local_ms,
					transc,
					t_logger,
					aws.BotoClientWrapper(c_ec2, dname))

			for r_spec in conf.get("update-route53", []):
				do_route53(
					r_spec,
					local_ms,
					transc,
					t_logger,
					aws.BotoClientWrapper(c_r53, dname))
	except Exception as e:
		exc = e
	else:
		exc = None

	return ( dname, t_logger.logs, exc )

def do_exec_domain (dname: str, conf: list[dict[str, Any]], event: str):
	local_ms = deepcopy(ms)
	local_ms.domain = dname

	exec_mat = init_exec_mat(conf, local_ms.format)
	do_exec_mat(exec_mat, event)

def do_exec ():
	if not run_param.enable_exec:
		return

	event = ms.daemon_state
	fs = list[futures.Future]()

	try:
		with mk_dexecutor() as dpool:
			for dname, dconf in fleet_conf.get("domains", {}).items():
				conf = dconf.get("exec")
				if conf:
					f = dpool.submit(do_exec_domain, dname, conf, event)
					fs.append(f)

			while fs:
				r = fs.pop(0)
				r.result()
	finally:
		fs_cancel_all(fs)

def do_init ():
	if not run_param.enable_init:
		return

	failed_domains = set[str]()
	fs = list[futures.Future]()

	try:
		with mk_dexecutor() as dpool:
			for dname, dconf in fleet_conf.get("domains", {}).items():
				f = dpool.submit(do_domain_init, dname, dconf)
				fs.append(f)

			try:
				fs_wait_all(fs)
			except TimeoutError:
				# FIXME: never reached as the `fs_wait_all()` call is not timed.
				fs_cancel_all(fs)
				# TODO: send SIGALRM to the threads in the pool. No way to do that
				# with python for now... Shoulda written this in C.

			while fs:
				r = fs.pop(0)

				result = r.result()
				dname = result[0]
				tlogs = result[1]
				exc = result[2]

				ms.transaction_log += tlogs
				if exc:
					failed_domains.add(dname)
					ms.error.append(traceback.format_exception(exc))
	finally:
		fs_cancel_all(fs)

	if failed_domains:
		msg = '''Domain(s) failed: {dnames}'''.format(
			dnames = ", ".join(failed_domains))
		with stdout_lock:
			sys.stderr.write(msg + os.linesep)

		raise DomainFailedError(failed_domains)

	hostname = fleet_conf.get("set-hostname")
	if hostname:
		try:
			hostname = ms.format(hostname)
			# this sets the transient hostname (not permanent)
			socket.sethostname(hostname)
		except Exception as e:
			# just print the exception as it's not mission-critical
			with stdout_lock:
				ec2fleetd.pexcept(e, "setting hostname")

def mk_notify_backend (
		session,
		kind: str,
		opts: dict[str, Any]) -> NotifyBackend:
	if "region" not in opts:
		opts["region"] = ms.placement_region

	match kind:
		case "ans-sqs":
			return aws.SQSNotifyBackend(session, opts)
		case "aws-sns":
			return aws.SNSNotifyBackend(session, opts)

def do_notify_domain (dname: str, nlist: Iterable[dict[str, Any]]):
	local_ms = deepcopy(ms)
	local_ms.domain = dname
	session = mk_profile()

	for conf in nlist:
		matrix = conf.get("matrix", magic.Notify.Matrix.DEFAULT_MATRIX)
		row = matrix.get(local_ms.daemon_state, magic.Notify.Matrix.DEFAULT_ROW)
		if not row.get("enabled"):
			continue

		mail_subject = magic.Notify.SUBJECT
		mail_body = magic.Notify.BODY

		env = conf.get("envelope")
		if env:
			mail_subject = env.get("subject")
			mail_body = env.get("body")

		mail_subject = local_ms.format(mail_subject)
		mail_body = local_ms.format(mail_body)

		agent = mk_notify_backend(
			session,
			conf["backend"],
			conf.get("options", {}))
		try:
			agent.post(mail_subject, mail_body)
		except Exception as e:
			# Notification failure is not critical by design
			with stdout_lock:
				ec2fleetd.pexcept(e, "sending notification")

def do_notify ():
	if not run_param.enable_notify:
		return

	fs = list[futures.Future]()

	try:
		with mk_dexecutor() as dpool:
			for dname, dconf in fleet_conf.get("domains", {}).items():
				conf = dconf.get("notify")
				if conf:
					f = dpool.submit(do_notify_domain, dname, conf)
					fs.append(f)

			while fs:
				r = fs.pop(0)
				r.result()
	finally:
		fs_cancel_all(fs)

def do_poll () -> bool:
	int_sched = None
	try:
		int_sched = mm.poll_int_sched()
	except:
		pass
	if not int_sched or not int_sched.valid():
		return True

	ms.interrupt_time = int_sched.time().isoformat()
	ms.interrupt_action = int_sched.action()

	with stdout_lock:
		sys.stderr.write("SPOT INTERRUPTION NOTICE RECEIVED !!!" + os.linesep)
		sys.stderr.write(str(int_sched) + os.linesep)
	return False

def report_ready ():
	now = datetime.datetime.now(datetime.UTC)
	elapsed = now - init_start

	msg = '''Init complete! ({elapsed:.3f}s)'''.format(
		elapsed = elapsed.total_seconds())
	sys.stderr.write(msg + os.linesep)

# FIXME
'''
I messed up. It's difficult to send SIGALRM to the threads in a
`ThreadPoolExecutor`.
'''
if fleet_conf.get("timeout") is not None:
	sys.stderr.write(
		'''"timeout" setting is currently ignored.''' + os.linesep)

ec = EC.OK
try:
	init_start = datetime.datetime.now(datetime.UTC)
	do_exec()
	do_init()
	signal.alarm(0) # cancels alarm

	ms.daemon_state = DaemonState.STARTED
	do_exec()
	report_ready()
	do_notify()
	sdn.notify("READY=1")

	if run_param.enable_poll:
		sdn.notify("STATUS=Polling interruption ...")
		while do_poll():
			time.sleep(magic.Code.POLL_INTERVAL)
		ms.daemon_state = DaemonState.INTERRUPTED
		sdn.notify("STATUS=SPOT INTERRUPTION NOTICE RECEIVED!!!")
except InterruptedError:
	sdn.notify("STATUS=Process interrupted")
	ms.daemon_state = DaemonState.STOPPING
except Exception as e:
	if e is TimeoutError:
		sdn.notify("STATUS=Init timed out")
	else:
		sdn.notify("STATUS=Daemon failed")
	ms.error.append(traceback.format_exception(e))

	ms.daemon_state = DaemonState.FAILED
	ec = EC.GENERIC_ERR
	with stdout_lock:
		ec2fleetd.pexcept(json.dumps(ms.error, indent = '\t'), "Daemon failed")
finally:
	sdn.notify("STOPPING=1")

	result = aws.clean_up_transc(run_param.transc_id)
	ms.transaction_log += result[0]
	if result[1]:
		ec2fleetd.pexcept(result[1], "Cleaning up transaction.")
		sys.stderr.write(
			"There should be some resources the daemon was unable to clean up" +
			os.linesep)

	do_exec()
	do_notify()

exit(EC.OK)
