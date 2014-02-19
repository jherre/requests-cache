#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    requests_cache.core
    ~~~~~~~~~~~~~~~~~~~

    Core functions for configuring cache and monkey patching ``requests``
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from time import sleep

import requests
from requests import Session as OriginalSession
from requests.hooks import dispatch_hook

from requests_cache import backends
from requests_cache.compat import str, basestring, urlparse

try:
    ver = tuple(map(int, requests.__version__.split(".")))
except ValueError:
    pass
else:
    # We don't need to dispatch hook in Requests <= 1.1.0
    if ver < (1, 2, 0):
        dispatch_hook = lambda key, hooks, hook_data, *a, **kw: hook_data
    del ver


class CachedSession(OriginalSession):
    """ Requests ``Sessions`` with caching support.
    """

    def __init__(self, cache_name='cache', backend=None, expire_after=None,
                 allowable_codes=(200,), allowable_methods=('GET',),
                 **backend_options):
        """
        :param cache_name: for ``sqlite`` backend: cache file will start with this prefix,
                           e.g ``cache.sqlite``

                           for ``mongodb``: it's used as database name
                           
                           for ``redis``: it's used as the namespace. This means all keys
                           are prefixed with ``'cache_name:'``
        :param backend: cache backend name e.g ``'sqlite'``, ``'mongodb'``, ``'redis'``, ``'memory'``.
                        (see :ref:`persistence`). Or instance of backend implementation.
                        Default value is ``None``, which means use ``'sqlite'`` if available,
                        otherwise fallback to ``'memory'``.
        :param expire_after: number of seconds after cache will be expired
                             or `None` (default) to ignore expiration
        :type expire_after: float
        :param allowable_codes: limit caching only for response with this codes (default: 200)
        :type allowable_codes: tuple
        :param allowable_methods: cache only requests of this methods (default: 'GET')
        :type allowable_methods: tuple
        :kwarg backend_options: options for chosen backend. See corresponding
                                :ref:`sqlite <backends_sqlite>`, :ref:`mongo <backends_mongo>` 
                                and :ref:`redis <backends_redis>` backends API documentation
        """
        backend_options['expire_after'] = expire_after
        if backend is None or isinstance(backend, basestring):
            self.cache = backends.create_backend(backend, cache_name,
                                                 backend_options)
        else:
            self.cache = backend

        self._cache_expire_after = expire_after
        self._cache_expire_after_override = {}
        self._cache_throttle = {}
        self._cache_allowable_codes = allowable_codes
        self._cache_allowable_methods = allowable_methods
        self._is_cache_disabled = False
        super(CachedSession, self).__init__()

    def send(self, request, **kwargs):
        if (self._is_cache_disabled
            or request.method not in self._cache_allowable_methods):
            response = super(CachedSession, self).send(request, **kwargs)
            response.from_cache = False
            return response

        cache_key = self.cache.create_key(request)

        def wait_for_throttle():
            requests_per_second = self._lookup_throttle(request.url)
            if requests_per_second is not None and requests_per_second > 0:
                # enventually we'll use the cache to distribute these
                # but for now just wait
                seconds_per_request = 1.0 / requests_per_second
                sleep(seconds_per_request)                
            
        def send_request_and_cache_response():
            wait_for_throttle()
            response = super(CachedSession, self).send(request, **kwargs)
            if response.status_code in self._cache_allowable_codes:
                self.cache.save_response(cache_key, response)
            response.from_cache = False
            return response

        response, timestamp = self.cache.get_response_and_time(cache_key)
        if response is None:
            return send_request_and_cache_response()

        expire_after = self._lookup_expire_after(request.url)
        if expire_after is not None:
            difference = datetime.utcnow() - timestamp
            if difference > timedelta(seconds=expire_after):
                self.cache.delete(cache_key)
                return send_request_and_cache_response()

        # dispatch hook here, because we've removed it before pickling
        response.from_cache = True
        response = dispatch_hook('response', request.hooks, response, **kwargs)
        return response

    def request(self, method, url, params=None, data=None, headers=None,
                cookies=None, files=None, auth=None, timeout=None,
                allow_redirects=True, proxies=None, hooks=None, stream=None,
                verify=None, cert=None):
        response = super(CachedSession, self).request(method, url, params, data,
                                                      headers, cookies, files,
                                                      auth, timeout,
                                                      allow_redirects, proxies,
                                                      hooks, stream, verify, cert)
        if self._is_cache_disabled:
            return response

        main_key = self.cache.create_key(response.request)
        for r in response.history:
            self.cache.add_key_mapping(
                self.cache.create_key(r.request), main_key
            )
        return response

    @contextmanager
    def cache_disabled(self):
        """
        Context manager for temporary disabling cache
        ::

            >>> s = CachedSession()
            >>> with s.cache_disabled():
            ...     s.get('http://httpbin.org/ip')
        """
        self._is_cache_disabled = True
        try:
            yield
        finally:
            self._is_cache_disabled = False

    def expire_after(self, url, expire_after=300):
        """
        Override the base expire_after setting by url prefix.  Will
        find the longest registered setting for each domain which
        will then be used for expiring such requests.  These overrides
        MUST BE lower than the default setting.
        """
        if not self._cache_expire_after:
            return None

        if expire_after > self._cache_expire_after or expire_after < 0:
            raise ValueError

        # a dictionary of lists of tuples
        p_url = urlparse(url)
        a = self._cache_expire_after_override[p_url.netloc] = []
        a.append((p_url.path, expire_after,))
        a.sort(key=lambda t: len(t[0]), reverse=True)
        self._cache_expire_after_override[p_url.netloc] = a
        
        return expire_after

    def _lookup_expire_after(self, url):
        if not self._cache_expire_after:
            return None

        p_url = urlparse(url)
        a = self._cache_expire_after_override.get(p_url.netloc, [])
        for path, secs in a:
            if p_url.path.startswith(path):
                return secs

        return self._cache_expire_after
        
    def throttle(self, url, requests_per_second):
        """
        Specify a throttle rate for requests to the given url.  All
        urls below the given url will be throttled at this rate.  To
        throttle an entire domain, provide the root url.
        """
        requests_per_second = float(requests_per_second)

        if requests_per_second < 0 or requests_per_second > 1000:
            raise ValueError

        # a dictionary of lists of tuples
        p_url = urlparse(url)
        a = self._cache_throttle[p_url.netloc] = []
        a.append((p_url.path, requests_per_second,))
        a.sort(key=lambda t: len(t[0]), reverse=True)
        self._cache_throttle[p_url.netloc] = a
        
        return requests_per_second

    def _lookup_throttle(self, url):
        p_url = urlparse(url)
        a = self._cache_throttle.get(p_url.netloc, [])
        for path, requests_per_second in a:
            if p_url.path.startswith(path):
                return requests_per_second

        return None
        
    def ignore_cgi(self, *names):
        """Adds the list of named cgi parameters to ignore to the backend."""
        self.cache.append_ignore_cgi(*names)

def install_cache(cache_name='cache', backend=None, expire_after=None,
                 allowable_codes=(200,), allowable_methods=('GET',),
                 session_factory=CachedSession, **backend_options):
    """
    Installs cache for all ``Requests`` requests by monkey-patching ``Session``

    Parameters are the same as in :class:`CachedSession`. Additional parameters:

    :param session_factory: Session factory. It should inherit :class:`CachedSession` (default)
    """
    if backend:
        backend_options['expire_after'] = expire_after
        backend = backends.create_backend(backend, cache_name, backend_options)
    _patch_session_factory(
        lambda : session_factory(cache_name=cache_name,
                                  backend=backend,
                                  allowable_codes=allowable_codes,
                                  allowable_methods=allowable_methods,
                                  **backend_options)
    )


# backward compatibility
configure = install_cache


def uninstall_cache():
    """ Restores ``requests.Session`` and disables cache
    """
    _patch_session_factory(OriginalSession)


@contextmanager
def disabled():
    """
    Context manager for temporary disabling globally installed cache

    .. warning:: not thread-safe

    ::

        >>> with requests_cache.disabled():
        ...     requests.get('http://httpbin.org/ip')
        ...     requests.get('http://httpbin.org/get')

    """
    previous = requests.Session
    uninstall_cache()
    try:
        yield
    finally:
        _patch_session_factory(previous)


@contextmanager
def enabled(*args, **kwargs):
    """
    Context manager for temporary installing global cache.

    Accepts same arguments as :func:`install_cache`

    .. warning:: not thread-safe

    ::

        >>> with requests_cache.enabled('cache_db'):
        ...     requests.get('http://httpbin.org/get')

    """
    install_cache(*args, **kwargs)
    try:
        yield
    finally:
        uninstall_cache()


def get_cache():
    """ Returns internal cache object from globally installed ``CachedSession``
    """
    return requests.Session().cache


def clear():
    """ Clears globally installed cache
    """
    get_cache().clear()

def expire_after(url, expire_after=300):
    """Sets the expire_after value for the url prefix in the globally 
    installed ``CacheSession``
    """
    s = requests.Session()
    if not isinstance(s, CachedSession):
        raise TypeError

    return s.expire_after(url, expire_after)

def throttle(url, requests_per_second):
    """Sets the throttle value for the url prefix in the globally 
    installed ``CacheSession``
    """
    s = requests.Session()
    if not isinstance(s, CachedSession):
        raise TypeError

    return s.throttle(url, requests_per_second)

def ignore_cgi(*names):
    """Adds to the list of ignored cgi parameters.
    """
    s = requests.Session()
    if not isinstance(s, CachedSession):
        raise TypeError

    return s.ignore_cgi(*names)

def _patch_session_factory(session_factory=CachedSession):
    requests.Session = requests.sessions.Session = session_factory
