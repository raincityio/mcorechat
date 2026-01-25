#!/bin/sh

fail() { echo $@>&2; exit 1; }

test -n "${remote}" || fail "remote not set"
#test -n "${jail}" || fail "jail not set"
root=$(realpath $(dirname $0))
test -n "${root}" || fail "root not found"
local=$(dirname ${root})
test -n "${root}" || fail "local not found"
project=$(basename ${root})
test -n "${project}" || fail "project not found"

#remote=polaris:/jails/${jail}.jail/root/root/src

gitfiles=`mktemp`
gitignorefiles=`mktemp`
trap "rm $gitfiles $gitignorefiles" EXIT
git -C ${root} ls-files|xargs -IPATH echo ${project}/PATH > ${gitfiles}
git -C ${root} ls-files --others --ignored --exclude-standard > ${gitignorefiles}
rsync -a -P --delete --files-from=${gitfiles} ${local} ${remote}/
