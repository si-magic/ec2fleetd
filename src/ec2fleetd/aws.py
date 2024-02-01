import datetime
import glob
import io
import json
import time
from abc import *
from typing import Any, override

import ec2imds

from ec2fleetd import *


class Magic:
	class TagName:
		TRANSC_ID = "user:ec2fd.transc-id"
		DOMAIN = "user:ec2fd.domain"
		TS_USED = "user:ec2fd.ts-used"
		POOL_NAME = "user:ec2fd.pool-name"
		IN_TRANSIT = "user:ec2fd.in-transit"
	class Code:
		EC2_VOL_CREATE_POLLWAIT_STEPS = [ 0.0, 1.0, 5.0, 5.0, 10.0 ]
		EC2_VOL_DETACH_WAIT = 1.0


class EC2VolumeCreatePollWaitStep:
	def __init__ (
				self,
				steps: Iterable[float] = Magic.Code.EC2_VOL_CREATE_POLLWAIT_STEPS):
		self._cur = 0
		self._steps = list[float](steps)

	def next (self, unused: int = None) -> float:
		ret = self._steps[self._cur]
		self._cur = min(self._cur + 1, len(self._steps) - 1)

		return ret

class EC2InterruptSchedule (InterruptSchedule):
	def __init__ (self, doc: dict[str, Any]):
		if doc is not None:
			self._doc = doc
			self._time = datetime.datetime.fromisoformat(self._doc["time"])
			self._action = self._doc["action"]
		else:
			self._doc = None
			self._time = None
			self._action = None

	def __bool__ (self) -> bool:
		return bool(self._doc)

	def __str__ (self) -> str:
		if self._doc:
			return json.dumps(self._doc)
		return super().__str__()

	def time (self) -> datetime.datetime | None:
		return self._time

	def action (self) -> str | None:
		return self._action

	def valid (self) -> bool:
		return datetime.datetime.now(datetime.UTC) <= self._time

class EC2MetaManager (MetaManager):
	def extract_ip_addresses (mo, ipv_name: str) -> Iterable[str] | None:
		try:
			for mac in mo["meta-data/network/interfaces/macs"].values():
				ret = mac.get(ipv_name)
				if ret:
					return ret
		except KeyError:
			pass

	def update_macroset (mo, ms: MacroSet):
		ms.instance_id = mo["meta-data/instance-id"]
		ms.instance_type = mo["meta-data/instance-type"]
		ms.instance_index = mo["meta-data/ami-launch-index"]
		ms.placement_region = mo["meta-data/placement/region"]
		ms.placement_zone = mo["meta-data/placement/availability-zone"]
		ms.hypervisor = mo["meta-data/system"]
		ms.primary_public_ipv4 = mo["meta-data/public-ipv4"]
		ms.primary_public_ipv6 = mo["meta-data/ipv6"]
		ms.public_ipv4_list = EC2MetaManager.extract_ip_addresses(mo, "public-ipv4s")
		ms.public_ipv6_list = EC2MetaManager.extract_ip_addresses(mo, "ipv6s")


	def __init__ (self, imds: str | None = None):
		if imds:
			imds_endpoints = ec2imds.IMDSWrapper.mk_endpoint_list_from_str(imds)
		else:
			imds_endpoints = ec2imds.IMDSAPIMagic.endpoints

		self._imds = ec2imds.IMDSWrapper(imds_endpoints)
		# The token callbacks are not set because the daemon is long-lived

	def fetch_meta (self, ms: MacroSet):
		ret = self._imds.all()
		EC2MetaManager.update_macroset(ret, ms)
		return ret

	def open_userdata (self) -> io.BufferedIOBase:
		ret = self._imds.open_userdata()

		if ret is None:
			return io.BytesIO()
		return ret

	def poll_int_sched (self) -> InterruptSchedule:
		obj = self._imds.dir_dict["meta-data/spot/instance-action"].func()
		return EC2InterruptSchedule(obj)

class AWSResourceTranscLog (ResourceTransactionLog):
	def __init__ (
			self,
			domain: str,
			method: str,
			param: dict[str, Any],
			dry: bool = False):
		self._domain = domain
		self._method = method
		self._param = param
		self._dry = dry

	@override
	def __repr__ (self) -> str:
		return str({
			"platform": "aws",
			"domain": self._domain,
			"method": self._method,
			"param": self._param,
			"dry": self._dry
		})

	def dict (self) -> dict:
		return {
			"platform": "aws",
			"domain": self._domain,
			"method": self._method,
			"param": self._param,
			"dry": self._dry
		}

	def dry (self) -> bool:
		return self._dry

class BotoClientWrapper:
	def __init__ (self, client, domain: str):
		self.client = client
		self._domain = domain

	def do_call (self, fname: str, logger: ResourceTransactionLogger, **kwargs):
		f = getattr(self.client, fname)

		log = AWSResourceTranscLog(
			self._domain,
			fname,
			kwargs,
			kwargs.get("DryRun", False))
		if logger:
			logger.publish([ log ])

		return f(**kwargs)

def _find_blockdev_by_vid_linux (vid) -> str | None:
	'''
	https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvme-ebs-volumes.html

	On Nitro instance,
	/sys/block/nvme0n1/device/serial: volNNNNNNNNNNNNNNNNN


	On other instances, assume the path in the config is the actual path in the
	guest (xvd* or sd*)

	Unlike NVMe volumes on Nitro, Xen drives do not expose their the volume id
	to the instance, so a bit of trial and error to figure out how your
	particular Linux distro or instance type to which you're trying to deploy
	behaves may be required.

	https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/device_naming.html
	https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/virtualization_types.html
	'''
	left = "/sys/block/"
	right = "/device/serial"
	dev_serials = glob.glob(left + "*" + right)
	vid = str(vid).replace('-', '')

	for path in dev_serials:
		with open(path) as f:
			if f.read().strip() == vid:
				return "/dev/" + path[len(left):][:-len(right)]

def _find_blockdev_by_vid_win (*args) -> str | None:
	raise NotImplementedError(
		"Trying to use this on Windows? You're f**ked, man.")

def _find_blockdev_by_vid_unknown (*args) -> str | None:
	raise NotImplementedError("Function not implemented for: " + sys.platform)

if sys.platform.startswith("linux"):
	_find_blockdev_by_vid_f = _find_blockdev_by_vid_linux
elif sys.platform.startswith("win"):
	_find_blockdev_by_vid_f = _find_blockdev_by_vid_win
else:
	_find_blockdev_by_vid_f = _find_blockdev_by_vid_unknown

def find_blockdev_by_vid (vid) -> str | None:
	return _find_blockdev_by_vid_f(vid)

def add_extra_tags (
		extra_tags: Iterable[dict[str, Any]],
		rtype: str,
		tag_spec: list[dict[str, Any]]):
	dst = None
	for spec in tag_spec:
		if spec.get("ResourceType") == rtype:
			dst = spec.get("Tags", [])
			break
	if not dst:
		dst = []
		spec = { "ResourceType": rtype, "Tags": dst }
		tag_spec.append(spec)

	dst += extra_tags
	spec["Tags"] = dst

def put_transc_tag (
		c: BotoClientWrapper,
		id: str,
		transc_id: str,
		logger: ResourceTransactionLogger):
	return c.do_call(
		"create_tags",
		logger,
		Resources = [ id ],
		Tags = [
			{
				"Key": Magic.TagName.TRANSC_ID,
				"Value": transc_id
			}
		])

def delete_transc_tag (
		c: BotoClientWrapper,
		id: str,
		logger: ResourceTransactionLogger):
	return c.do_call(
		"delete_tags",
		logger,
		Resources = [ id ],
		Tags = [
			{ "Key": Magic.TagName.TRANSC_ID },
			{ "Key": Magic.TagName.IN_TRANSIT }
		])

class EC2CreatedVolumeHold (ResourceHold):
	def __init__ (
			self,
			c: BotoClientWrapper,
			id: str,
			logger: ResourceTransactionLogger):
		self._c = c
		self._id = id
		self._logger = logger

	def commit (self):
		delete_transc_tag(self._c, self._id, self._logger)

	def rollback (self):
		return self._c.do_call("delete_volume", self._logger, VolumeId = self._id)

class EC2AttachedVolumeHold (ResourceHold):
	def __init__ (
			self,
			c: BotoClientWrapper,
			id: str,
			transc_id: str,
			logger: ResourceTransactionLogger):
		self._c = c
		self._id = id
		self._logger = logger

		put_transc_tag(c, id, transc_id, logger)

	def commit (self):
		delete_transc_tag(self._c, self._id, self._logger)

	def rollback (self):
		delete_transc_tag(self._c, self._id, self._logger)

		rsp = self._c.do_call(
			"detach_volume",
			self._logger,
			VolumeId = self._id,
			Force = True)

		while rsp["State"] in [ "in-use", "detaching" ]:
			time.sleep(Magic.Code.EC2_VOL_DETACH_WAIT)

			rsp = self._c.do_call(
				"describe_volumes",
				self._logger,
				VolumeIds = [ self._id ])["Volumes"]
			if not rsp: # GONE!
				break
			rsp = rsp[0]

		return rsp

def mk_r53_rrchanges (
		action: str,
		rrs: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
	return [ { "Action": action, "ResourceRecordSet": rr } for rr in rrs ]

class Route53InsertedRRHold (ResourceHold):
	def __init__ (
			self,
			c: BotoClientWrapper,
			hostedzone_id: str,
			rrs: dict[str, Any],
			logger: ResourceTransactionLogger):
		self._c = c
		self._hzi = hostedzone_id
		self._rrs = rrs
		self._logger = logger

	def commit (self):
		pass

	def rollback (self):
		param = {
			"HostedZoneId": self._hzi,
			"ChangeBatch": { "Changes": mk_r53_rrchanges("DELETE", self._rrs) }
		}
		return self._c.do_call("change_resource_record_sets", self._logger, **param)

class Route53UpdatedRRHold (ResourceHold):
	def __init__ (
			self,
			c: BotoClientWrapper,
			hostedzone_id: str,
			saved: dict[str, Any],
			logger: ResourceTransactionLogger):
		self._c = c
		self._hzi = hostedzone_id
		self._rrs = saved
		self._logger = logger

	def commit (self):
		pass

	def rollback (self):
		param = {
			"HostedZoneId": self._hzi,
			"ChangeBatch": { "Changes": mk_r53_rrchanges("UPSERT", self._rrs) }
		}
		return self._c.do_call("change_resource_record_sets", self._logger, **param)

def _init_common_post_client_opts (opts: dict[str, Any]) -> dict[str, Any]:
	kwargs = dict[str, Any]()
	region = opts.get("region")
	if region:
		kwargs["region_name"] = region

	return kwargs

class SNSNotifyBackend (NotifyBackend):
	def __init__ (self, session, opts: dict[str, Any]):
		self._client = session.client(
			"sns",
			**_init_common_post_client_opts(opts))
		self._topic = opts["topic"]

	def post (self, subject: str, body: str):
		return self._client.publish(
			TopicArn = self._topic,
			Subject = subject,
			Message = body)

class SQSNotifyBackend (NotifyBackend):
	def __init__ (self, session, opts: dict[str, Any]):
		self._client = session.client(
			"sqs",
			**_init_common_post_client_opts(opts))
		self._q_url = opts["queue-url"]

	def post (self, subject: str, body: str):
		return self._client.send_message(
			QueueUrl = self._q_url,
			MessageBody = body)

def clean_up_transc (
		transc_id: str) -> tuple[Iterable[ResourceTransactionLog], Exception]:
	# TODO
	'''
	This function left unimplemented for the moment.

	The function is for cleaning up any resources `TransientResourceManager`
	failed to clean up upon rollback.

	It should do following:

	- Get all resources with "user:ec2fd.in-transit"(`Magic.TagName.IN_TRANSIT`)
	  flag set. For each resource ...
	  - Detach/Disassociate if required
	  - Delete the resource

	I didn't bother implementing it because the only time
	`TransientResourceManager` fails to clean up after itself is when the init
	process times out and the main process has to kill the init process/thread.
	'''
	return ( [], None )
