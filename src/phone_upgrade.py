"""Explicit update: stable phone command, atomic replacement, paired payload."""
import hashlib
from pathlib import Path
import struct
import re


def payload(resources, config):
    token = config.get('token', '')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
        raise ValueError('Invalid pairing for phone update')
    data = (Path(resources) / 'phone_network_v3.py').read_bytes()
    marker = b'__PHONE_PAIRING_SECRET__'
    if data.count(marker) != 1:
        raise ValueError('Invalid phone update template')
    data = data.replace(marker, token.encode())
    if not 0 < len(data) < 65536:
        raise ValueError('Phone update size is invalid')
    compile(data, 'phone_network.py', 'exec')
    return data


def receiver_command(resources, config):
    digest = hashlib.sha256(payload(resources, config)).hexdigest()
    # The checksum is public; no phone token is placed on the clipboard.
    return ('python3 -c \'import socket,struct,hashlib,os,shutil,pathlib; os.umask(63); s=socket.socket(); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
            's.bind(("127.0.0.1",18764)); s.listen(1); s.settimeout(180); '
            'print("READY FOR PHONE UPDATE",flush=True); c,_=s.accept(); c.settimeout(60); '
            'f=c.makefile("rb"); n=struct.unpack("!I",f.read(4))[0]; '
            'assert 0<n<65536; b=f.read(n); '
            f'assert len(b)==n and hashlib.sha256(b).hexdigest()=="{digest}","Checksum failed"; '
            'compile(b,"phone_network.py","exec"); '
            't="/root/phone_network.py"; v=t+".backup"; '
            'shutil.copyfile(t,v) if os.path.exists(t) and pathlib.Path(t).read_bytes()!=b else None; '
            'os.chmod(v,384) if os.path.exists(v) else None; '
            'q=open(t+".new","wb"); q.write(b); q.flush(); os.fsync(q.fileno()); q.close(); os.chmod(t+".new",384); '
            'os.replace(t+".new",t); '
            'c.sendall(b"OK"); f.close(); c.close(); s.close(); '
            'print("Saved. Run: python3 /root/phone_network.py",flush=True)\'')


def send(config, resources):
    import pc_gateway as pc
    import win_support as win
    if win.connection_active():
        raise RuntimeError('A phone connection is running. Disconnect it yourself before updating.')
    data = payload(resources, config)
    with pc.usb.connect_phone(config, 18764) as connection:
        connection.sendall(struct.pack('!I', len(data)) + data)
        if pc.protocol.exact(connection, 2) != b'OK':
            raise RuntimeError('Phone did not acknowledge the verified update')
    return 'Saved and checksum verified. In iSH run: python3 /root/phone_network.py. Then click Check phone in this new app.'
