"""Force acceptance failure in disposable VM, exercising real data/image rollback."""
import os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,'/opt/shmax-hostd/lib')
from shmax.host.local_update import LocalReleaseUpdater, LocalUpdateError
class RejectedCandidate(LocalReleaseUpdater):
    def ready(self, host, port, container, *, integrations=True):
        if integrations:
            # Simulate changes made by a candidate before it fails readiness.
            (self.data/'sessions/max/bootstrap-acceptance.session').write_bytes(b'rejected candidate changes')
            c=sqlite3.connect(self.data/'db/shmax.db');c.execute("update bootstrap_acceptance set value='rejected'");c.commit();c.close()
            return False
        return super().ready(host,port,container,integrations=False)
try:
    RejectedCandidate().apply(Path('/tmp/candidate/shmax-v0.1.0.tar.gz'))
except LocalUpdateError as exc:
    if 'previous image restored' not in str(exc):raise
    print('EXPECTED REJECTION: verified cold-data and original-image rollback completed')
else:raise SystemExit('Expected acceptance rejection did not occur')
