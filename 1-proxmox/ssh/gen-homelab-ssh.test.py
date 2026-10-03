import importlib.util, pathlib, subprocess, sys

here = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location("gen", here / "gen-homelab-ssh.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

dns = """# header
# ssh-user=ubuntu
address=/k3s-master-51.epaflix.lan/192.168.10.51
address=/no-ssh.epaflix.lan/192.168.10.99
# ssh-user=root
# unrelated comment resets the user
address=/takaros.epaflix.lan/192.168.10.10
# ssh-user=root
address=/public.epaflix.com/192.168.10.5
# ssh-user=spy
address=/homepc.epaflix.lan/192.168.10.177
"""
assert gen.hosts(dns) == [("k3s-master-51", "192.168.10.51", "ubuntu"), ("homepc", "192.168.10.177", "spy")]
out = gen.render(gen.hosts(dns))
assert "Host k3s-master-51\n    HostName 192.168.10.51\n    User ubuntu\n" in out
assert out.endswith("Match final host 192.168.10.51,192.168.10.177\n    Include ~/.ssh/homelab-identity.conf\n")
try:
    gen.hosts(dns + "# ssh-user=x\naddress=/homepc.epaflix.lan/192.168.10.2\n")
    raise AssertionError("duplicate accepted")
except SystemExit:
    pass
# The committed file is current.
subprocess.run([sys.executable, str(here / "gen-homelab-ssh.py"), "--check"], check=True)
print("gen-homelab-ssh: all checks passed")
