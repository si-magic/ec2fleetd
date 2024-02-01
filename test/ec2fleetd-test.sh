#!/bin/bash
set -e

declare -A R_TAGS
R_TAGS[transc-id]="user:ec2fd.transc-id"
R_TAGS[domain]="user:ec2fd.domain"
R_TAGS[ts-used]="user:ec2fd.ts-used"
R_TAGS[ts-created]="user:ec2fd.ts-created"
R_TAGS[pool-name]="user:ec2fd.pool-name"

declare -A CMD
CMD[aws]="aws"
CMD[jq]="jq"

# 1: count
wait_all () {
	cnt="$1"
	for (( i = 0; i < cnt; i += 1 ))
	do
		wait
	done
}

mk_tmpdir () {
	declare TDIR=$(mktemp -d /tmp/ec2fleetd-test.XXXXXXXXXX)
	echo "$TDIR"
}

rm_tmpdir () {
	rm -rf "$TDIR"
}

mk_tmp () {
	mktemp "$TDIR/XXXXXXXXXX"
}

# 1: domain
dta_all_vol () {
	local f_vol=$(mk_tmp)
	local cnt

	# get attached volumes
	${CMD[aws]} ec2 describe-volumes \
		--no-paginate \
		--filters	"Name=tag:user:ec2fd.domain,Values=$1" \
					"Name=status,Values=in-use" |
	${CMD[jq]} '.Volumes[].VolumeId' \
	> "$f_vol"

	# request forceful detachment
	cnt=0
	while read vid
	do
		${CMD[aws]} ec2 detach-volume --force --volume-id $vid &
		let 'cnt += 1'
	done < "$f_vol"
	wait_all $cnt

	# wait for the op to finish
	while true
	do
		cnt=$(${CMD[aws]} ec2 describe-volumes \
				--no-paginate \
				--filters	"Name=tag:user:ec2fd.domain,Values=$1" \
							"Name=status,Values=in-use" |
			${CMD[jq]} '.Volumes | length')

		[ "$cnt" -eq 0 ] && break
	done
}

# 1: domain
del_all_vol () {
	local f_vol=$(mk_tmp)
	local cnt

	dta_all_vol $@

	# get all volumes in domain
	${CMD[aws]} ec2 describe-volumes \
		--no-paginate \
		--filters "Name=tag:user:ec2fd.domain,Values=$1" |
	${CMD[jq]} '.Volumes[].VolumeId' \
	> "$f_vol"

	# request deletion
	cnt=0
	while read vid
	do
		${CMD[aws]} ec2 delete-volume --volume-id $vid &
		let 'cnt += 1'
	done < "$f_vol"
	wait_all $cnt
}

# 1: hostedzone_id
# rest: list of record names
del_all_r53 () {
	local f_changes=$(mk_tmp)
	local hz="$1"
	shift
	local pat="$@"

	pat="${pat// /|}"
	pat="${pat//./\.}"
	pat="^($pat)[.]?$"

	${CMD[aws]} route53 list-resource-record-sets --hosted-zone-id "$hz" |
		jq '{ "Changes":
			[{
				"Action": "DELETE",
				"ResourceRecordSet": .ResourceRecordSets[] | select(.Name | test("'$pat'"))
			}] }' \
		> "$f_changes"
	${CMD[aws]} route53 change-resource-record-sets \
		--hosted-zone-id "$hz" \
		--change-batch "$(cat "$f_changes")"
}


cmd_test () {
	export PYTHONPATH=$(realpath ../src)
	pushd "$1"
	. script
}

cmd_clean_volumes () {
	for d in $@
	do
		del_all_vol "$d"
	done
}

cmd_clean_rrs () {
	del_all_r53 $@
}


cmd=$1
shift

mk_tmpdir

set +e
"cmd_$cmd" $@ &
EC=$?

rm_tmpdir
exit $EC
