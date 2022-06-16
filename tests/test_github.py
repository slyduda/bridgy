"""Unit tests for github.py."""
import copy

import granary
import granary.tests.test_github as gr_test_github
import oauth_dropins
from oauth_dropins.webutil.util import json_dumps, json_loads

import github
from . import testutil


class GitHubTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    user = copy.deepcopy(gr_test_github.USER_GRAPHQL)
    user['login'] = 'Snarfed'
    self.auth_entity = oauth_dropins.github.GitHubAuth(
      id='Snarfed', access_token_str='towkin',
      user_json=json_dumps(user))

    self.auth_entity.put()
    self.gh = github.GitHub.new(self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.gh.auth_entity.get())
    self.assertEqual('snarfed', self.gh.key.id())
    self.assertEqual('Snarfed', self.gh.label_name())
    self.assertEqual('Ryan Barrett', self.gh.name)
    self.assertEqual('https://github.com/Snarfed', self.gh.silo_url())
    self.assertEqual('https://avatars2.githubusercontent.com/u/778068?v=4',
                     self.gh.picture)
    self.assertEqual('tag:github.com,2013:MDQ6VXNlcjc3ODA2OA==', self.gh.user_tag_id())
