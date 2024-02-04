class Code:
	DEVICE_WAIT = 0.01 # 10ms
	INIT_TIMEOUT = 600 # 1 minute
	POLL_INTERVAL = 1 # 1 second

class Notify:
	class Matrix:
		DEFAULT_ROW = { "enabled": True }
		DEFAULT_MATRIX = {
			"failed": DEFAULT_ROW,
			"started": DEFAULT_ROW,
			"stopping": DEFAULT_ROW,
			"interrupted": DEFAULT_ROW
	}
	SUBJECT = '''Fleetd {domain} on {instance_id} state changed to [{daemon_state}]'''
	BODY = '''{all_json}'''

SupportedHypervisors = set([ "xen", "nitro" ])

def is_supported_hv (v: str) -> bool:
	if not v:
		return False

	v = v.lower()
	for hv in SupportedHypervisors:
		# accommodate for values like "xen-on-nitro" or "nitro-on-something"
		if v.startswith(hv):
			return True

	return False
