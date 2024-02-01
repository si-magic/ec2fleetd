#!/bin/bash
if [ -z "$1" ]
then
	echo "Usage: $0 <outpath> <string>" >&2
	exit 2
fi

echo -n "$2" > "$1"
