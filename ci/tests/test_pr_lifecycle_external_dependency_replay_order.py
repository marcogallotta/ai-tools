import unittest
from datetime import timedelta
import test_pr_lifecycle as b
from test_pr_lifecycle_external_dependency import external_dependency_comment as c
p=b.pr_lifecycle
class ReplayOrderCase(unittest.TestCase):
 def test_old_resolution_does_not_clear_new_blocker(self):
  a=c(owner_pr=77,when=b.NOW-timedelta(minutes=2),comment_id=80)
  z=c(owner_pr=88,evidence="issue%3A99",when=b.NOW-timedelta(minutes=1),comment_id=81)
  r=c(action="resolved",owner_pr=77,when=b.NOW,comment_id=82)
  x=p.parse_external_dependency([a,z,r])
  self.assertEqual((x.owner_pr,x.evidence),(88,"issue:99"))
