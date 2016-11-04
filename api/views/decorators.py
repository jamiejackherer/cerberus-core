# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016, OVH SAS
#
# This file is part of Cerberus-core.
#
# Cerberus-core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
    Decorators for Cerberus protected API.
"""

import datetime
import json

from functools import wraps
from time import mktime

from django.conf import settings
from flask import g, request
from voluptuous import Invalid, MultipleInvalid, Schema
from werkzeug.contrib.cache import RedisCache, SimpleCache
from werkzeug.exceptions import BadRequest, Forbidden

from api.controllers import GeneralController
from utils import logger

Logger = logger.get_logger(__name__)

DEFAULT_CACHE_TIMEOUT = 300

Cache = None
USE_CACHE = False

if settings.API['use_cache']:
    USE_CACHE = True
    if settings.API['cache_engine'] == 'redis':
        Cache = RedisCache(**settings.REDIS)
    elif settings.API['cache_engine'] == 'memory':
        Cache = SimpleCache()
    else:
        raise Exception('Unsupported cache engine %s' % settings.API['cache_engine'])

Schemas = {}


class Cached(object):
    """
        Return cached response, update if timeout
    """
    def __init__(self, timeout=None, current_user=False):
        self.timeout = timeout or DEFAULT_CACHE_TIMEOUT
        self.current_user = current_user
        self.__name__ = 'cache'

    def __call__(self, func):
        @wraps(func)
        def decorator(*args, **kwargs):
            user = g.user.id if self.current_user else None
            route = '%s,%s,%s' % (request.path, json.dumps(request.args), user)
            if not USE_CACHE:
                return func(*args, **kwargs)
            response = Cache.get(unicode(route))
            if response is None:
                response = func(*args, **kwargs)
                Cache.set(route, response, self.timeout)
            else:
                Logger.debug(unicode('get %s from cache, timeout %d' % (route, self.timeout)))
            return response
        return decorator


class InvalidateCache(object):
    """
        Return cached response, update if timeout
    """
    def __init__(self, routes, args=None, clear_for_user=False):
        self.routes = routes
        self.args = args if args else {}
        self.clear_for_user = clear_for_user
        self.__name__ = 'cache'

    def __call__(self, func):

        @wraps(func)
        def decorator(*args, **kwargs):
            response = func(*args, **kwargs)
            if not USE_CACHE:
                return response
            for path in self.routes:
                route = '%s,%s,%s' % (path, json.dumps(self.args), None)
                Cache.delete(unicode(route))
                user = kwargs.get('user', None) if self.clear_for_user else None
                Logger.debug(unicode('clear %s from cache' % (route)))
                if user:
                    route = '%s,%s,%s' % (path, json.dumps(self.args), user)
                    Cache.delete(unicode(route))
                    Logger.debug(unicode('clear %s from cache' % (route)))
            return response
        return decorator


class TimestampJSONEncoder(json.JSONEncoder):
    """
        JSONEncoder subclass that convert datetime to timestamp
    """
    def default(self, obj):
        # See "Date Time String Format" in the ECMA-262 specification.
        if isinstance(obj, datetime.datetime):
            timestamp = int(mktime(obj.timetuple()))
            return timestamp
        else:
            return super(TimestampJSONEncoder, self).default(obj)


def admin_required(func):
    """ Check if user is admin
    """
    @wraps(func)
    def check_admin(*args, **kwargs):
        if not g.user.operator.role.codename == 'admin':
            raise Forbidden('Forbidden')
        return func(*args, **kwargs)
    return check_admin


def perm_required(func):
    """ Check if user can do actions
    """
    @wraps(func)
    def check_perm(*args, **kwargs):
        if 'report' in kwargs:
            GeneralController.check_perms(
                method=request.method,
                user=g.user,
                report=kwargs['report']
            )
        if 'ticket' in kwargs:
            GeneralController.check_perms(
                method=request.method,
                user=g.user,
                ticket=kwargs['ticket']
            )
        if 'defendant' in kwargs and request.method != 'GET':
            GeneralController.check_perms(
                method=request.method,
                user=g.user,
                defendant=kwargs['defendant']
            )
        return func(*args, **kwargs)
    return check_perm


def validate_body(schema_desc):
    """
        Validate json body
    """
    def real_decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                body = request.get_json()
                if not Schemas.get(func.__name__):
                    Schemas[func.__name__] = Schema(schema_desc, required=True)
                    Logger.debug(unicode('registering schema for %s' % (func.__name__)))
                Schemas[func.__name__](body)
            except (Invalid, MultipleInvalid) as ex:
                Logger.error(unicode(ex))
                msg = 'Missing or invalid field(s) in body, expecting {}'.format(schema_desc)
                raise BadRequest(msg)
            return func(*args, **kwargs)
        return wrapper
    return real_decorator
