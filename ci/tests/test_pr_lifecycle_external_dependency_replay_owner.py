import unittest
from datetime import timedelta
import test_pr_lifecycle as b
from test_pr_lifecycle_external_dependency import external_dependency_comment as c
p=b.pr_lifecycle
class ReplayIdentityCase(unittest.TestCase):
 def test_owner_pr_mismatch(self):
  a=c(when=b.NOW-timedelta(minutes=1),comment_id=80)
  r=c(action="resolved",owner_pr=88,when=b.NOW,comment_id=81)
  self.assertEqual(p.parse_external_dependency([a,r]).owner_pr,77)
