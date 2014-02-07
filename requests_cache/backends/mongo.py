#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    requests_cache.backends.mongo
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    ``mongo`` cache backend
"""
from .base import BaseCache
from .storage.mongodict import MongoDict, MongoPickleDict


class MongoCache(BaseCache):
    """ ``mongo`` cache backend.
    """
    def __init__(self, **options):
        """
        :param db_name: database name (default: ``'requests-cache'``)
        :param connection: (optional) ``pymongo.MongoClient``
        """
        super(MongoCache, self).__init__()
        db_name = options.get('db_name', 'requests_cache')
        db_collection = options.get('db_collection', 'responses')
        expire_after = options.get('expire_after', 300)
        self.responses = MongoPickleDict(db_name, db_collection,
                                         options.get('connection'))
        self.keys_map = MongoDict(db_name, collection_name='urls', 
                                  connection=self.responses.connection,
                                  expire_after=expire_after)
