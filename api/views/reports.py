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
    Report views for Cerberus protected API.
"""

from io import BytesIO

from flask import Blueprint, g, request, send_file

from api.controllers import ReportItemsController, ReportsController
from decorators import perm_required

report_views = Blueprint('report_views', __name__)


@report_views.route('/api/reports', methods=['GET'])
def get_all_reports():
    """ Get abuse reports

        Filtering is possible through "filters" query string, JSON double encoded format
    """
    resp, _ = ReportsController.index(filters=request.args.get('filters'), user=g.user)
    return resp


@report_views.route('/api/reports/<report>', methods=['GET'])
@perm_required
def get_report(report=None):
    """ Get a given report
    """
    return ReportsController.show(report)


@report_views.route('/api/reports/<report>', methods=['PUT', 'DELETE'])
@perm_required
def update_report(report=None):
    """ Update a given report
    """
    if request.method == 'PUT':
        body = request.get_json()
        return ReportsController.update(report, body, g.user)
    else:
        return ReportsController.destroy(report)


@report_views.route('/api/reports/<report>/items', methods=['GET'])
@perm_required
def get_report_items(report=None):
    """ Get all items for a given report

        Filtering is possible through "filters" query string, JSON double encoded format
    """
    return ReportItemsController.get_items_report(rep=report, filters=request.args.get('filters'))


@report_views.route('/api/reports/<report>/items', methods=['POST'])
@perm_required
def create_report_item(report=None):
    """ Add item to report
    """
    body = request.get_json()
    body['report'] = report
    return ReportItemsController.create(body, g.user)


@report_views.route('/api/reports/<report>/items/<item>', methods=['PUT', 'DELETE'])
@perm_required
def update_report_item(report=None, item=None):
    """ Update an item
    """
    if request.method == 'PUT':
        body = request.get_json()
        body['report'] = report
        return ReportItemsController.update(item, body, g.user)
    else:
        return ReportItemsController.delete_from_report(item, report, g.user)


@report_views.route('/api/reports/<report>/items/<item>/screenshots', methods=['GET'])
@perm_required
def get_item_screenshot(report=None, item=None):
    """ Get available screenshots for given item
    """
    return ReportItemsController.get_screenshot(item, report)


@report_views.route('/api/reports/<report>/items/screenshots', methods=['GET'])
@perm_required
def get_all_items_screenshot(report=None):
    """ Get all available screenshots for given report
    """
    return ReportsController.get_items_screenshot(
        report=report,
        filters=request.args.get('filters')
    )


@report_views.route('/api/reports/<report>/items/<item>/unblock', methods=['POST'])
@perm_required
def unblock_report_item(report=None, item=None):
    """ Unblock an item
    """
    return ReportItemsController.unblock_item(item_id=item, report_id=report)


@report_views.route('/api/reports/<report>/raw', methods=['GET'])
@perm_required
def get_raw_report(report=None):
    """ Get raw email for a report
    """
    return ReportsController.get_raw(report)


@report_views.route('/api/reports/<report>/dehtmlify', methods=['GET'])
@perm_required
def get_dehtmlified_report(report=None):
    """ Get raw email for a report
    """
    return ReportsController.get_dehtmlified(report)


@report_views.route('/api/reports/<report>/attachments', methods=['GET'])
@perm_required
def get_all_report_attachments(report=None):
    """ Get attached documents for a report
    """
    resp, _ = ReportsController.get_all_attachments(
        report=report,
        filters=request.args.get('filters')
    )
    return resp


@report_views.route('/api/reports/<report>/attachments/<attachment>', methods=['GET'])
@perm_required
def get_report_attachment(report=None, attachment=None):
    """ Get attached documents for a report
    """
    resp = ReportsController.get_attachment(report, attachment)
    bytes_io = BytesIO(resp['raw'])
    return send_file(
        bytes_io,
        attachment_filename=resp['filename'],
        mimetype=resp['filetype'],
        as_attachment=True
    )


@report_views.route('/api/reports/<report>/tags', methods=['POST'])
@perm_required
def add_report_tag(report=None):
    """ Add tag to report
    """
    body = request.get_json()
    return ReportsController.add_tag(report, body)


@report_views.route('/api/reports/<report>/tags/<tag>', methods=['DELETE'])
@perm_required
def delete_report_tag(report=None, tag=None):
    """ Delete report tag
    """
    return ReportsController.remove_tag(report, tag)


@report_views.route('/api/reports/bulk', methods=['PUT', 'DELETE'])
@perm_required
def bulk_add_reports():
    """ Bulk add on reports
    """
    body = request.get_json()
    if request.method == 'PUT':
        return ReportsController.bulk_add(body, g.user, request.method)
    else:
        return ReportsController.bulk_delete(body, g.user, request.method)


@report_views.route('/api/reports/<report>/feedback', methods=['POST'])
@perm_required
def post_feedback(report=None):
    """ Post feeback
    """
    body = request.get_json()
    return ReportsController.parse_screenshot_feedback(report, body, g.user)
