"""Sketch analyzer plugin for the Safe Browsing API."""
from __future__ import unicode_literals

import fnmatch
import logging
import re

import pkg_resources
import requests
from flask import current_app

from timesketch.lib.analyzers import interface
from timesketch.lib.analyzers import manager


class SafeBrowsingSketchPlugin(interface.BaseSketchAnalyzer):
    """Sketch analyzer for Safe Browsing."""

    NAME = 'safebrowsing'

    # Safe Browsing API v4, threatMatches.find endpoint.
    _SAFE_BROWSING_THREATMATCHING_ENDPOINT = (
        'https://safebrowsing.googleapis.com/v4/threatMatches:find')

    # Maximal number of URLs in a single Lookup request as per the
    # Safe Browsing API documentation.
    _SAFE_BROWSING_BULK_LIMIT = 500

    # An optional file containing URL wildcards to be whitelisted
    # in a YAML file.
    _URL_WHITELIST_CONFIG = 'safebrowsing_whitelist.yaml'

    # The keys to be added to the TS event from the ThreatMatch object
    # we get from Safe Browsing API.
    _SAFEBROWSING_ENTRY_KEEP = frozenset([
        'platformType',
        'threatType',
    ])

    # Used to find proper URLs in the 'url' entries of TS events.
    _URL_BEGINNING_RE = re.compile(r'(http(s|):\/\/\S*)')

    def __init__(self, index_name, sketch_id):
        """Initialize The Sketch Analyzer.

        Args:
            index_name: Elasticsearch index name
            sketch_id: Sketch ID
        """
        super(SafeBrowsingSketchPlugin, self).__init__(index_name, sketch_id)

        self._safebrowsing_api_key = current_app.config.get(
            'SAFEBROWSING_API_KEY')

        self._google_client_id = current_app.config.get(
            'SAFEBROWSING_CLIENT_ID',
            'Timesketch',
        )

        self._google_client_version = current_app.config.get(
            'SAFEBROWSING_CLIENT_VERSION',
            pkg_resources.get_distribution('timesketch').version,
        )

    def _is_url_whitelisted(self, url, whitelist):
        """Does a glob-match against the whitelist.

        Args:
            url: The url
            whitelist: The whitelist, list-like
        Returns:
            Boolean with the result
        """

        for url_pattern in whitelist:
            if fnmatch.fnmatchcase(url, url_pattern):
                return True

        return False

    def _do_safebrowsing_lookup(self, urls, platforms, types):
        """URL lookup against the Safe Browsing API.

        Args:
            urls: URLs
            platforms: platformTypes field of threatInfo
            types: threatTypes field of threatInfo
        Returns:
            Dict of URLs with the hits
        """
        results = {}

        api_client = {
            'clientId': self._google_client_id,
            'clientVersion': self._google_client_version,
        }

        for index in range(0, len(urls), self._SAFE_BROWSING_BULK_LIMIT):
            body = {
                'client': api_client,
                'threatInfo': {
                    'platformTypes': platforms,
                    'threatTypes': types,
                    'threatEntryTypes': ['URL'],
                    'threatEntries': [
                        {'url': url}
                        for url in urls[
                            index:index + self._SAFE_BROWSING_BULK_LIMIT]
                    ],
                },
            }

            response = requests.post(
                self._SAFE_BROWSING_THREATMATCHING_ENDPOINT,
                params={'key': self._safebrowsing_api_key},
                json=body,
            )

            response.raise_for_status()

            result = response.json()

            if not result:
                continue

            if 'matches' not in result:
                continue

            for match in result.get('matches'):
                result_url = match.get('threat', {}).get('url')

                if not result_url:
                    continue

                # Removing all key/values that are not defined in
                # the _SAFEBROWSING_ENTRY_KEEP.
                threat_result = match.copy()
                for key in match.keys():
                    if key not in self._SAFEBROWSING_ENTRY_KEEP:
                        threat_result.pop(key)

                results[result_url] = threat_result

        return results

    def _sanitize_url(self, url_entry):
        """Finds http[s]:// in 'url_entry' and returns its content from there.

        Args:
            url_entry: a URL, with some other characters before and after

        Returns:
            String with the URL or empty string if not found
        """
        m = self._URL_BEGINNING_RE.search(url_entry)

        if m:
            return m.group(1)

        return ''

    def run(self):
        """Entry point for the analyzer.

        Returns:
            String with summary of the analyzer result
        """
        # Exit ASAP if the API key is missing.
        if not self._safebrowsing_api_key:
            return 'Safe Browsing API requires an API key!'

        query = (
            '{"query": { "bool": { "should": [ '
            '{ "exists" : { "field" : "url" }} ] } } }')

        return_fields = ['url']

        events = self.event_stream(
            query_dsl=query,
            return_fields=return_fields,
        )

        urls = {}

        for event in events:
            url = self._sanitize_url(event.source.get('url'))

            if not url:
                continue

            urls.setdefault(url, []).append(event)

        # Exit early if there are no URLs in the data set to analyze.
        if not urls:
            return 'No URLs to analyze.'

        url_whitelisted = 0

        url_whitelist = set(
            interface.get_yaml_config(
                self._URL_WHITELIST_CONFIG,
            ),
        )

        if not url_whitelist:
            domain_analyzer_whitelisted = current_app.config.get(
                'DOMAIN_ANALYZER_WHITELISTED_DOMAINS',
                [],
            )
            for domain in domain_analyzer_whitelisted:
                url_whitelist.add('*.%s/*' % domain)

        logging.info(
            '{0:d} entries on the whitelist.'.format(len(url_whitelist)),
        )

        safebrowsing_platforms = current_app.config.get(
            'SAFEBROWSING_PLATFORMS',
            ['ANY_PLATFORM'],
        )

        safebrowsing_types = current_app.config.get(
            'SAFEBROWSING_THREATTYPES',
            ['MALWARE'],
        )

        lookup_urls = []

        for url in urls:
            if self._is_url_whitelisted(url, url_whitelist):
                url_whitelisted += 1
                continue

            lookup_urls.append(url)

        try:
            safebrowsing_results = self._do_safebrowsing_lookup(
                lookup_urls,
                safebrowsing_platforms,
                safebrowsing_types,
            )
        except requests.HTTPError:
            return 'Couldn\'t reach the Safe Browsing API.'

        for url, events in urls.items():
            if url not in safebrowsing_results:
                continue

            safebrowsing_result = safebrowsing_results[url]
            for event in events:
                tags = ['google-safebrowsing-url']

                threat_type = safebrowsing_result.get('threatType')

                if threat_type:
                    tags.append(
                        'google-safebrowsing-%s' % threat_type.lower(),
                    )

                event.add_tags(tags)
                event.add_attributes(
                    {
                        'google-safebrowsing-threat': safebrowsing_result,
                    },
                )
                event.commit()

        return (
            '{0:d} Safe Browsing result(s) on {1:d} URL(s), '
            '{2:d} whitelisted.').format(
                len(safebrowsing_results),
                len(urls),
                url_whitelisted,
            )


manager.AnalysisManager.register_analyzer(SafeBrowsingSketchPlugin)
