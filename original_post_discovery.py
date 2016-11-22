"""Augments the standard original_post_discovery algorithm with a
reverse lookup that supports posts without a backlink or citation.

Performs a reverse-lookup that scans the activity's author's h-feed
for posts with rel=syndication links. As we find syndicated copies,
save the relationship.  If we find the original post for the activity
in question, return the original's URL.

See http://indiewebcamp.com/posse-post-discovery for more detail.

This feature adds costs in terms of HTTP requests and database
lookups in the following primary cases:

- Author's domain is known to be invalid or blacklisted, there will
  be 0 requests and 0 DB lookups.

- For a syndicated post has been seen previously (regardless of
  whether discovery was successful), there will be 0 requests and 1
  DB lookup.

- The first time a syndicated post has been seen:
  - 1 to 2 HTTP requests to get and parse the h-feed plus 1 additional
    request for *each* post permalink that has not been seen before.
  - 1 DB query for the initial check plus 1 additional DB query for
    *each* post permalink.
"""

import collections
import datetime
import itertools
import logging
import mf2util
import requests
import urlparse
import util

from granary import microformats2
from granary import source as gr_source
from google.appengine.api.datastore import MAX_ALLOWABLE_QUERIES
import models
from models import SyndicatedPost

from google.appengine.api import memcache

MAX_PERMALINK_FETCHES = 10


def discover(source, activity, fetch_hfeed=True, include_redirect_sources=True,
             already_fetched_hfeeds=None):
  """Augments the standard original_post_discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  If fetch_hfeed is False, then we will check the db for previously
  found SyndicatedPosts but will not do posse-post-discovery to find
  new ones.

  Args:
    source: models.Source subclass. Changes to property values (e.g. domains,
      domain_urls, last_syndication_url) are stored in source.updates; they
      should be updated transactionally later.
    activity: activity dict
    fetch_hfeed: boolean
    include_redirect_sources: boolean, whether to include URLs that redirect as
      well as their final destination URLs
    already_fetched_hfeeds: set, URLs that we have already fetched and run
      posse-post-discovery on, so we can avoid running it multiple times

  Returns:
    (set(string original post URLs), set(string mention URLs)) tuple

  """
  if not source.updates:
    source.updates = {}

  if already_fetched_hfeeds is None:
    already_fetched_hfeeds = set()

  originals, mentions = gr_source.Source.original_post_discovery(
    activity, domains=source.domains, cache=memcache,
    include_redirect_sources=include_redirect_sources,
    headers=util.USER_AGENT_HEADER)

  obj = activity.get('object', {})
  author_id = obj.get('author', {}).get('id') or activity.get('author', {}).get('id')
  if author_id and author_id != source.user_tag_id():
    logging.info(
      "Demoting original post links because user %s doesn't match author %s",
      source.user_tag_id(), author_id)
    # this is someone else's post, so all links must be mentions
    mentions.update(originals)
    originals = set()

  # look for original URL of attachments (e.g. quote tweets)
  for att in obj.get('attachments', []):
    if (att.get('objectType') in ('note', 'article')
        and att.get('author', {}).get('id') == source.user_tag_id()):
      logging.debug('running original post discovery on attachment: %s',
                    att.get('id'))
      att_origs, _ = discover(
        source, att, include_redirect_sources=include_redirect_sources)
      logging.debug('original post discovery found originals for attachment, %s',
                    att_origs)
      mentions.update(att_origs)

  def resolve(urls):
    resolved = set()
    for url in urls:
      final, _, send = util.get_webmention_target(url)
      if send:
        resolved.add(final)
        if include_redirect_sources:
          resolved.add(url)
    return resolved

  originals = resolve(originals)
  mentions = resolve(mentions)

  if not source.get_author_urls():
    logging.debug('no author url(s), cannot find h-feed')
    return originals, mentions

  # TODO possible optimization: if we've discovered a backlink to a post on the
  # author's domain (i.e., it included a link or citation), then skip the rest
  # of this.
  syndication_url = obj.get('url') or activity.get('url')
  if syndication_url:
    # use the canonical syndication url on both sides, so that we have
    # the best chance of finding a match. Some silos allow several
    # different permalink formats to point to the same place (e.g.,
    # facebook user id instead of user name)
    syndication_url = source.canonicalize_url(syndication_url)
    if syndication_url:
      originals.update(_posse_post_discovery(
        source, activity, syndication_url, fetch_hfeed,
        already_fetched_hfeeds))
    originals = set(util.dedupe_urls(originals))
  else:
    logging.debug('no syndication url, cannot process h-entries')

  return originals, mentions


def refetch(source):
  """Refetch the author's URLs and look for new or updated syndication
  links that might not have been there the first time we looked.

  Args:
    source: models.Source subclass. Changes to property values (e.g. domains,
      domain_urls, last_syndication_url) are stored in source.updates; they
      should be updated transactionally later.

  Return:
    a dict of syndicated_url to a list of new models.SyndicatedPosts
  """
  if not source.updates:
    source.updates = {}

  logging.debug('attempting to refetch h-feed for %s', source.label())
  results = {}
  for url in _get_author_urls(source):
    results.update(_process_author(source, url, refetch=True))

  return results


def targets_for_response(resp, originals, mentions):
  """Returns the URLs that we should send webmentions to for a given response.

  ...specifically, all responses except posts get sent to original post URLs,
  but only posts and comments get sent to mentioned URLs.

  Args:
    resp: ActivityStreams response object
    originals, mentions: sequence of string URLs

  Returns:
    set of string URLs
  """
  type = models.Response.get_type(resp)
  targets = set()
  if type != 'post':
    targets |= originals
  if type in ('post', 'comment'):
    targets |= mentions
  return targets


def _posse_post_discovery(source, activity, syndication_url, fetch_hfeed,
                          already_fetched_hfeeds):
  """Performs the actual meat of the posse-post-discover.

  Args:
    source: models.Source subclass
    activity: activity dict
    syndication_url: url of the syndicated copy for which we are
      trying to find an original
    fetch_hfeed: boolean, whether or not to fetch and parse the
      author's feed if we don't have a previously stored
      relationship
    already_fetched_hfeeds: set, URLs we've already fetched in a
      previous iteration

  Return:
    sequence of string original post urls, possibly empty
  """
  logging.info('starting posse post discovery with syndicated %s',
               syndication_url)

  relationships = SyndicatedPost.query(
    SyndicatedPost.syndication == syndication_url,
    ancestor=source.key).fetch()

  if not relationships and fetch_hfeed:
    # a syndicated post we haven't seen before! fetch the author's URLs to see
    # if we can find it.
    #
    # TODO: Consider using the actor's url, with get_author_urls() as the
    # fallback in the future to support content from non-Bridgy users.
    results = {}
    for url in _get_author_urls(source):
      if url not in already_fetched_hfeeds:
        results.update(_process_author(source, url))
        already_fetched_hfeeds.add(url)
      else:
        logging.debug('skipping %s, already fetched this round', url)

    relationships = results.get(syndication_url, [])

  if not relationships:
    # No relationships were found. Remember that we've seen this
    # syndicated post to avoid reprocessing it every time
    logging.debug('posse post discovery found no relationship for %s',
                  syndication_url)
    if fetch_hfeed:
      SyndicatedPost.insert_syndication_blank(source, syndication_url)

  originals = [r.original for r in relationships if r.original]
  if originals:
    logging.debug('posse post discovery found relationship(s) %s -> %s',
                  syndication_url, originals)
  return originals


def _process_author(source, author_url, refetch=False, store_blanks=True):
  """Fetch the author's domain URL, and look for syndicated posts.

  Args:
    source: a subclass of models.Source
    author_url: the author's homepage URL
    refetch: boolean, whether to refetch and process entries we've seen before
    store_blanks: boolean, whether we should store blank SyndicatedPosts when
      we don't find a relationship

  Return:
    a dict of syndicated_url to a list of new models.SyndicatedPost
  """
  # for now use whether the url is a valid webmention target
  # as a proxy for whether it's worth searching it.
  author_url, _, ok = util.get_webmention_target(author_url)
  if not ok:
    return {}

  try:
    logging.debug('fetching author url %s', author_url)
    author_resp = util.requests_get(author_url)
    # TODO for error codes that indicate a temporary error, should we make
    # a certain number of retries before giving up forever?
    author_resp.raise_for_status()
    author_dom = util.beautifulsoup_parse(author_resp.text)
  except AssertionError:
    raise  # for unit tests
  except BaseException:
    # TODO limit allowed failures, cache the author's h-feed url
    # or the # of times we've failed to fetch it
    logging.warning('Could not fetch author url %s', author_url, exc_info=True)
    return {}

  feeditems = _find_feed_items(author_url, author_dom)

  # look for all other feed urls using rel='feed', type='text/html'
  feed_urls = set()
  for rel_feed_node in (author_dom.find_all('link', rel='feed')
                        + author_dom.find_all('a', rel='feed')):
    feed_url = rel_feed_node.get('href')
    if not feed_url:
      continue

    feed_url = urlparse.urljoin(author_url, feed_url)
    feed_type = rel_feed_node.get('type')
    if not feed_type:
      # type is not specified, use this to confirm that it's text/html
      feed_url, _, feed_type_ok = util.get_webmention_target(feed_url)
    else:
      feed_type_ok = feed_type == 'text/html'

    if feed_url == author_url:
      logging.debug('author url is the feed url, ignoring')
    elif not feed_type_ok:
      logging.debug('skipping feed of type %s', feed_type)
    else:
      feed_urls.add(feed_url)

  for feed_url in feed_urls:
    try:
      logging.debug("fetching author's rel-feed %s", feed_url)
      feed_resp = util.requests_get(feed_url)
      feed_resp.raise_for_status()
      logging.debug("author's rel-feed fetched successfully %s", feed_url)
      feeditems = _merge_hfeeds(feeditems,
                                _find_feed_items(feed_url, feed_resp.text))

      domain = util.domain_from_link(feed_url)
      if source.updates is not None and domain not in source.domains:
        domains = source.updates.setdefault('domains', source.domains)
        if domain not in domains:
          logging.info('rel-feed found new domain %s! adding to source', domain)
          domains.append(domain)

    except AssertionError:
      raise  # reraise assertions for unit tests
    except BaseException:
      logging.warning('Could not fetch h-feed url %s.', feed_url,
                      exc_info=True)

  # sort by dt-updated/dt-published
  def updated_or_published(item):
    props = microformats2.first_props(item.get('properties'))
    return props.get('updated') or props.get('published')

  feeditems.sort(key=updated_or_published, reverse=True)

  permalink_to_entry = collections.OrderedDict()
  for child in feeditems:
    if 'h-entry' in child['type']:
      for permalink in child['properties'].get('url', []):
        if isinstance(permalink, basestring):
          permalink_to_entry[permalink] = child
        else:
          logging.warn('unexpected non-string "url" property: %s', permalink)

    if len(permalink_to_entry) >= MAX_PERMALINK_FETCHES:
      logging.info('Hit cap of %d permalinks. Stopping.', MAX_PERMALINK_FETCHES)
      break

  # query all preexisting permalinks at once, instead of once per link
  permalinks_list = list(permalink_to_entry.keys())
  # fetch the maximum allowed entries (currently 30) at a time
  preexisting_list = itertools.chain.from_iterable(
    SyndicatedPost.query(
      SyndicatedPost.original.IN(permalinks_list[i:i + MAX_ALLOWABLE_QUERIES]),
      ancestor=source.key)
    for i in xrange(0, len(permalinks_list), MAX_ALLOWABLE_QUERIES))
  preexisting = {}
  for r in preexisting_list:
    preexisting.setdefault(r.original, []).append(r)

  results = {}
  for permalink, entry in permalink_to_entry.iteritems():
    logging.debug('processing permalink: %s', permalink)
    new_results = _process_entry(
      source, permalink, entry, refetch, preexisting.get(permalink, []),
      store_blanks=store_blanks)
    for key, value in new_results.iteritems():
      results.setdefault(key, []).extend(value)

  if source.updates is not None and results:
    # keep track of the last time we've seen rel=syndication urls for
    # this author. this helps us decide whether to refetch periodically
    # and look for updates.
    # Source will be saved at the end of each round of polling
    source.updates['last_syndication_url'] = util.now_fn()

  return results


def _merge_hfeeds(feed1, feed2):
  """Merge items from two h-feeds into a composite feed. Skips items in
  feed2 that are already represented in feed1, based on the "url" property.

  Args:
    feed1: a list of dicts
    feed2: a list of dicts

  Returns:
    a list of dicts
  """
  seen = set()
  for item in feed1:
    for url in item.get('properties', {}).get('url', []):
      if isinstance(url, basestring):
        seen.add(url)

  return feed1 + [item for item in feed2 if all(
    url not in seen for url in item.get('properties', {}).get('url', []))]


def _find_feed_items(feed_url, feed_doc):
  """Extract feed items from a given URL and document. If the top-level
  h-* item is an h-feed, return its children. Otherwise, returns the
  top-level items.

  Args:
    feed_url: a string. the URL passed to mf2py parser
    feed_doc: a string or BeautifulSoup object. document is passed to
      mf2py parser

  Returns:
    a list of dicts, each one representing an mf2 h-* item
  """
  parsed = util.mf2py_parse(feed_doc, feed_url)

  feeditems = parsed['items']
  hfeeds = mf2util.find_all_entries(parsed, ('h-feed',))
  if hfeeds:
    feeditems = list(itertools.chain.from_iterable(
      hfeed.get('children', []) for hfeed in hfeeds))
  else:
    logging.debug('No h-feed found, fallback to top-level h-entrys.')
  return feeditems


def _process_entry(source, permalink, feed_entry, refetch, preexisting,
                   store_blanks=True):
  """Fetch and process an h-entry, saving a new SyndicatedPost to the
  DB if successful.

  Args:
    source:
    permalink: url of the unprocessed post
    feed_entry: the h-feed version of the h-entry dict, often contains
      a partial version of the h-entry at the permalink
    refetch: boolean, whether to refetch and process entries we've seen before
    preexisting: a list of previously discovered models.SyndicatedPosts
      for this permalink
    store_blanks: boolean, whether we should store blank SyndicatedPosts when
      we don't find a relationship

  Returns:
    a dict from syndicated url to a list of new models.SyndicatedPosts
  """
  # if the post has already been processed, do not add to the results
  # since this method only returns *newly* discovered relationships.
  if preexisting:
    # if we're refetching and this one is blank, do not return.
    # if there is a blank entry, it should be the one and only entry,
    # but go ahead and check 'all' of them to be safe.
    if not refetch:
      return {}
    synds = [s.syndication for s in preexisting if s.syndication]
    if synds:
      logging.debug('previously found relationship(s) for original %s: %s',
                    permalink, synds)

  # first try with the h-entry from the h-feed. if we find the syndication url
  # we're looking for, we don't have to fetch the permalink
  permalink, _, type_ok = util.get_webmention_target(permalink)
  usynd = feed_entry.get('properties', {}).get('syndication', [])
  if usynd:
    logging.debug('u-syndication links on the h-feed h-entry: %s', usynd)
  results = _process_syndication_urls(source, permalink, set(
    url for url in usynd if isinstance(url, basestring)), preexisting)
  success = True

  if results:
    source.updates['last_feed_syndication_url'] = util.now_fn()
  elif not source.last_feed_syndication_url:
    # fetch the full permalink page if we think it might have more details
    parsed = None
    try:
      logging.debug('fetching post permalink %s', permalink)
      if type_ok:
        resp = util.requests_get(permalink)
        resp.raise_for_status()
        parsed = util.mf2py_parse(resp.text, permalink)
    except AssertionError:
      raise  # for unit tests
    except BaseException:
      # TODO limit the number of allowed failures
      logging.warning('Could not fetch permalink %s', permalink, exc_info=True)
      success = False

    if parsed:
      syndication_urls = set()
      relsynd = parsed.get('rels').get('syndication', [])
      if relsynd:
        logging.debug('rel-syndication links: %s', relsynd)
      syndication_urls.update(url for url in relsynd
                              if isinstance(url, basestring))
      # there should only be one h-entry on a permalink page, but
      # we'll check all of them just in case.
      for hentry in (item for item in parsed['items']
                     if 'h-entry' in item['type']):
        usynd = hentry.get('properties', {}).get('syndication', [])
        if usynd:
          logging.debug('u-syndication links: %s', usynd)
        syndication_urls.update(url for url in usynd
                                if isinstance(url, basestring))
      results = _process_syndication_urls(
        source, permalink, syndication_urls, preexisting)

  # detect and delete SyndicatedPosts that were removed from the site
  if success:
    result_syndposts = itertools.chain(*results.values())
    for syndpost in list(preexisting):
      if syndpost.syndication and syndpost not in result_syndposts:
        logging.info('deleting relationship that disappeared: %s', syndpost)
        syndpost.key.delete()
        preexisting.remove(syndpost)

  if not results:
    logging.debug('no syndication links from %s to current source %s.',
                  permalink, source.label())
    results = {}
    if store_blanks and not preexisting:
      # remember that this post doesn't have syndication links for this
      # particular source
      logging.debug('saving empty relationship so that %s will not be '
                    'searched again', permalink)
      SyndicatedPost.insert_original_blank(source, permalink)

  # only return results that are not in the preexisting list
  new_results = {}
  for syndurl, syndposts_for_url in results.iteritems():
    for syndpost in syndposts_for_url:
      if syndpost not in preexisting:
        new_results.setdefault(syndurl, []).append(syndpost)

  if new_results:
    logging.debug('discovered relationships %s', new_results)
  return new_results


def _process_syndication_urls(source, permalink, syndication_urls,
                              preexisting):
  """Process a list of syndication URLs looking for one that matches the
  current source.  If one is found, stores a new SyndicatedPost in the
  db.

  Args:
    source: a models.Source subclass
    permalink: a string. the current h-entry permalink
    syndication_urls: a collection of strings. the unfitered list
      of syndication urls
    preexisting: a list of previously discovered SyndicatedPosts

  Returns:
    dict mapping string syndication url to list of SyndicatedPost
  """
  results = {}
  # save the results (or lack thereof) to the db, and put them in a
  # map for immediate use
  for url in syndication_urls:
    # source-specific logic to standardize the URL. (e.g., replace facebook
    # username with numeric id)
    url = source.canonicalize_url(url)
    if not url:
      continue

    # TODO: save future lookups by saving results for other sources too (note:
    # query the appropriate source subclass by author.domains, rather than
    # author.domain_urls)
    #
    # we may have already seen this relationship, save a DB lookup by
    # finding it in the preexisting list
    relationship = next((sp for sp in preexisting
                         if sp.syndication == url
                         and sp.original == permalink), None)
    if not relationship:
      logging.debug('saving discovered relationship %s -> %s', url, permalink)
      relationship = SyndicatedPost.insert(
        source, syndication=url, original=permalink)
    results.setdefault(url, []).append(relationship)

  return results


def _get_author_urls(source):
  max = models.MAX_AUTHOR_URLS
  urls = source.get_author_urls()
  if len(urls) > max:
    logging.warning('user has over %d URLs! only running PPD on %s. skipping %s.',
                    max, urls[:max], urls[max:])
    urls = urls[:max]

  return urls
