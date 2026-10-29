#!/bin/sh
# HTTPS for the panel: a certificate authority of its own (made once; the phone installs ca.crt and then
# trusts the panel) and a server certificate for ADDRESS, renewed when the address changes or the
# certificate runs out within 30 days. Both programs run this on start and whenever the address changes.
# The authority may only vouch for local addresses (nameConstraints), so its key in certs/ cannot be used
# to impersonate other sites to the phone.
#
#   certificate.sh ADDRESS
set -e
address=$1
cd "$(dirname "$0")"
mkdir -p certs
cd certs
if [ -f ca.crt ] && [ -f server.crt ] && openssl x509 -in server.crt -noout -checkend 2592000 >/dev/null &&
   openssl x509 -in server.crt -noout -text | grep -Eq "IP Address:$address *\$"; then
    exit 0
fi
umask 077
cat > openssl.cnf <<END
[req]
distinguished_name = dn
[dn]
[ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical, permitted;IP:10.0.0.0/255.0.0.0, permitted;IP:172.16.0.0/255.240.0.0, permitted;IP:192.168.0.0/255.255.0.0, permitted;IP:127.0.0.0/255.0.0.0, permitted;DNS:localhost
[server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
authorityKeyIdentifier = keyid
subjectAltName = DNS:localhost, IP:127.0.0.1, IP:$address
END
if [ ! -f ca.crt ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout ca.key -out ca.crt \
        -days 3650 -subj "/CN=Ergometer $(hostname -s)" -config openssl.cnf -extensions ca 2>/dev/null
fi
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout server.key.new -out server.csr \
    -subj "/CN=Ergometer" -config openssl.cnf 2>/dev/null
# 397 days: browsers reject server certificates that are valid for longer.
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -set_serial "0x$(openssl rand -hex 16)" -days 397 \
    -extfile openssl.cnf -extensions server -out server.crt.new 2>/dev/null
mv server.key.new server.key
mv server.crt.new server.crt
rm -f server.csr
