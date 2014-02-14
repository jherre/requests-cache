#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    requests_cache.backends.redis
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    ``redis`` cache backend
"""
from .base import BaseCache
from .storage.redisdict import RedisDict


class RedisCache(BaseCache):
    """ ``redis`` cache backend.
    """
    def __init__(self, **options):
        """
        :param namespace: redis namespace (default: ``'requests-cache'``)
        :param connection: (optional) ``redis.StrictRedis``
        """
        super(RedisCache, self).__init__(**options)
        namespace = options.get('namespace', 'requests-cache')
        self.responses = RedisDict(namespace, 'responses',
                                         options.get('connection'))
        self.keys_map = RedisDict(namespace, 'urls', self.responses.connection)
