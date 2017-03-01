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
    Ticket functions for worker
"""

import operator

from datetime import datetime, timedelta
from time import mktime, sleep, time

from django.conf import settings
from django.db.models import Q, ObjectDoesNotExist

import common
import database
import phishing

from abuse.models import (Comment, ContactedProvider, Report,
                          Tag, Ticket, TicketComment,
                          BusinessRules, ServiceActionJob, User)
from factory.implementation import ImplementationFactory as implementations
from utils import utils
from workflows.actions import ReportActions
from workflows.engine import run
from workflows.variables import ReportVariables
from worker import Logger

ASYNC_JOB_TO_CANCEL = (
    'action.apply_if_no_reply',
    'action.apply_then_close',
    'action.apply_action',
    'ticket.timeout',
)

WAITING = 'WaitingAnswer'
PAUSED = 'Paused'
ALARM = 'Alarm'

STATUS_SEQUENCE = [WAITING, ALARM, WAITING]


def delay_jobs(ticket=None, delay=None, back=True):
    """
        Delay pending jobs for given `abuse.models.Ticket`

        :param `abuse.models.Ticket` ticket: The Cerberus ticket
        :param int delay: Postpone duration
        :param bool back: In case of unpause, reschedule jobs with effectively elapsed time
    """
    if not delay:
        Logger.error(unicode('Missing delay. Skipping...'))
        return

    if not isinstance(ticket, Ticket):
        try:
            ticket = Ticket.objects.get(id=ticket)
        except (AttributeError, ObjectDoesNotExist, TypeError, ValueError):
            Logger.error(unicode('Ticket %d cannot be found in DB. Skipping...' % (ticket)))
            return

    # a job is here a tuple (Job instance, datetime instance)
    pending_jobs = utils.scheduler.get_jobs(until=timedelta(days=7), with_times=True)
    pending_jobs = {job[0].id: job for job in pending_jobs}

    for job in ticket.jobs.all():
        if pending_jobs.get(job.asynchronousJobId):
            current_date = pending_jobs[job.asynchronousJobId][1]
            new_date = current_date - delay if back else current_date + delay
            utils.scheduler.change_execution_time(
                pending_jobs[job.asynchronousJobId][0],
                new_date
            )


# XXX: rewrite
def timeout(ticket_id=None):
    """
        If ticket timeout , apply action on service (if defendant not internal/VIP)
        and ticket is not assigned

        :param int ticket_id: The id of the Cerberus `abuse.models.Ticket`
    """
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except (AttributeError, ObjectDoesNotExist, ValueError):
        Logger.error(unicode('Ticket %d cannot be found in DB. Skipping...' % (ticket_id)))
        return

    if not _check_timeout_ticket_conformance(ticket):
        return

    action = implementations.instance.get_singleton_of(
        'ActionServiceBase'
    ).get_action_for_timeout(ticket)
    if not action:
        Logger.error(unicode('Ticket {} service {}: action not found, exiting ...'.format(
            ticket_id,
            ticket.service.componentType
        )))
        return

    # Maybe customer fixed, closing ticket
    if ticket.category.name.lower() == 'phishing' and phishing.is_all_down_for_ticket(ticket):
        Logger.info(unicode('All items are down for ticket %d, closing ticket' % (ticket_id)))
        _close_ticket(ticket, reason=settings.CODENAMES['fixed_customer'], service_blocked=False)
        return

    # Getting ip for action
    ip_addr = get_ip_for_action(ticket)
    if not ip_addr:
        Logger.error(unicode('Error while getting IP for action, exiting'))
        common.set_ticket_status(ticket, 'ActionError', reset_snooze=True)
        comment = Comment.objects.create(
            user=common.BOT_USER,
            comment='None or multiple ip addresses for this ticket'
        )
        TicketComment.objects.create(ticket=ticket, comment=comment)
        database.log_action_on_ticket(
            ticket=ticket,
            action='add_comment'
        )
        ticket.save()
        return

    # Apply action
    service_action_job = _apply_timeout_action(ticket, ip_addr, action)
    if not service_action_job.result:
        Logger.debug(unicode('Error while executing service action, exiting'))
        return

    Logger.info(unicode('All done, sending close notification to provider(s)'))
    ticket = Ticket.objects.get(id=ticket.id)

    # Closing ticket
    _close_ticket(ticket, reason=settings.CODENAMES['fixed'], service_blocked=True)


def get_ip_for_action(ticket):
    """
        Return the IP address attached to given ̀`abuse.models.Ticket`.
        If multiple are detected, return None

        :param `abuse.models.Ticket` ticket: The `abuse.models.Ticket` instance
        :return: The IP address
        :rtype: str
    """
    reports = ticket.reportTicket.all()

    ips = []
    for rep in reports:
        items = rep.reportItemRelatedReport.filter(
            ~Q(ip=None),
            itemType='IP'
        )
        for itm in items:
            ips.append(itm.ip)
        items = rep.reportItemRelatedReport.filter(
            ~Q(fqdnResolved=None),
            itemType__in=('FQDN', 'URL')
        )
        for itm in items:
            ips.append(itm.fqdnResolved)

    ips = list(set(ips))

    if len(ips) != 1:
        Logger.error(unicode('Multiple or no IP on this ticket'))
        return

    return ips[0]


def _check_timeout_ticket_conformance(ticket):

    if not ticket.defendant or not ticket.service:
        Logger.error(unicode(
            'Ticket %d is invalid (no defendant/service), skipping...' % (ticket.id))
        )
        return False

    if ticket.status.lower() in ['closed', 'answered']:
        Logger.error(unicode(
            'Ticket %d is invalid (no defendant/service or not Alarm), Skipping...' % (ticket.id))
        )
        return False

    if ticket.category.name.lower() not in ['phishing', 'copyright', 'illegal']:
        Logger.error(unicode(
            'Ticket %d is in wrong category (%s, Skipping...' % (ticket.id, ticket.category.name))
        )
        return False

    if ticket.treatedBy:
        Logger.error(unicode('Ticket is %d assigned, skipping' % (ticket.id)))
        return False

    if ticket.jobs.count():
        Logger.error(unicode('Ticket %d has existing jobs, exiting ...' % (ticket.id)))
        return False

    return True


def _apply_timeout_action(ticket, ip_addr, action):

    Logger.info(unicode('Executing action %s for ticket %d' % (action.name, ticket.id)))
    ticket.action = action
    database.log_action_on_ticket(
        ticket=ticket,
        action='set_action',
        action_name=action.name
    )
    ticket.save()
    async_job = utils.scheduler.schedule(
        scheduled_time=datetime.utcnow() + timedelta(seconds=3),
        func='action.apply_action',
        kwargs={
            'ticket_id': ticket.id,
            'action_id': action.id,
            'ip_addr': ip_addr,
            'user_id': common.BOT_USER.id,
        },
        interval=1,
        repeat=1,
        result_ttl=500,
        timeout=3600,
    )

    Logger.info(unicode('Task has %s job id' % (async_job.id)))
    job = ServiceActionJob.objects.create(
        ip=ip_addr,
        action=action,
        asynchronousJobId=async_job.id,
        creationDate=datetime.now()
    )
    ticket.jobs.add(job)

    while not async_job.is_finished:
        sleep(5)

    return async_job


def _close_ticket(ticket, reason=settings.CODENAMES['fixed_customer'], service_blocked=False):
    """
        Close ticket and add autoclosed Tag
    """
    # Send "case closed" email to already contacted Provider(s)
    providers_emails = ContactedProvider.objects.filter(
        ticket_id=ticket.id
    ).values_list(
        'provider__email',
        flat=True
    ).distinct()

    common.send_email(
        ticket,
        providers_emails,
        settings.CODENAMES['case_closed'],
    )

    if service_blocked:
        template = settings.CODENAMES['service_blocked']
    else:
        template = settings.CODENAMES['ticket_closed']

    # Send "ticket closed" email to defendant
    common.send_email(
        ticket,
        [ticket.defendant.details.email],
        template,
        lang=ticket.defendant.details.lang
    )

    _add_timeout_tag(ticket)
    common.close_ticket(ticket, resolution_codename=reason)


def _add_timeout_tag(ticket):

    tag_name = None

    if ticket.category.name.lower() == 'phishing':
        tag_name = settings.TAGS['phishing_autoclosed']
    elif ticket.category.name.lower() == 'copyright':
        tag_name = settings.TAGS['copyright_autoclosed']

    if tag_name:
        ticket.tags.add(Tag.objects.get(name=tag_name))
        ticket.save()

        database.log_action_on_ticket(
            ticket=ticket,
            action='add_tag',
            tag_name=tag_name
        )


def create_ticket_from_phishtocheck(report=None, user=None):
    """
        Re-apply "phishing_up" rules for validated PhishToCheck report

        :param int report: The id of the `abuse.models.Report`
        :param int user: The id of the `abuse.models.User`
    """
    report = Report.objects.get(id=report)
    user = User.objects.get(id=user)

    rule = BusinessRules.objects.get(name='phishing_up')
    config = rule.config

    conditions = []
    for cond in config['conditions']['all']:
        if cond['name'] not in ('all_items_phishing', 'urls_down'):
            conditions.append(cond)

    config['conditions']['all'] = conditions

    variables = ReportVariables(None, report, None, is_trusted=True)
    actions = ReportActions(report, None, 'EN')

    rule_applied = run(
        config,
        defined_variables=variables,
        defined_actions=actions,
    )

    if not rule_applied:
        raise AssertionError("Rule 'phishing_up' not applied")

    database.log_action_on_ticket(
        ticket=report.ticket,
        action='validate_phishtocheck',
        user=user,
        report=report
    )


def cancel_rq_scheduler_jobs(ticket_id=None, status='answered'):
    """
        Cancel all rq scheduler jobs for given `abuse.models.Ticket`

        :param int ticket_id: The id of the `abuse.models.Ticket`
        :param str status: The `abuse.models.Ticket.TICKET_STATUS' reason of the cancel
    """
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except (AttributeError, ObjectDoesNotExist, TypeError, ValueError):
        Logger.error(unicode('Ticket %d cannot be found in DB. Skipping...' % (ticket)))
        return

    for job in ticket.jobs.all():
        if job.asynchronousJobId in utils.scheduler:
            utils.scheduler.cancel(job.asynchronousJobId)
            job.status = 'cancelled by %s' % status
            job.save()

    for job in utils.scheduler.get_jobs():
        if job.func_name in ASYNC_JOB_TO_CANCEL and job.kwargs['ticket_id'] == ticket.id:
            utils.scheduler.cancel(job.id)


def close_emails_thread(ticket_id=None):
    """
    """
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except (AttributeError, ObjectDoesNotExist, TypeError, ValueError):
        Logger.error(unicode('Ticket %d cannot be found in DB. Skipping...' % (ticket)))
        return

    implementations.instance.get_singleton_of(
        'MailerServiceBase'
    ).close_thread(ticket)


def follow_the_sun():
    """
        Set tickets to alarm when user is away
    """
    now = int(time())
    where = [~Q(status='Open'), ~Q(status='Reopened'), ~Q(status='Paused'), ~Q(status='Closed')]
    where = reduce(operator.and_, where)

    for user in User.objects.filter(~Q(username=common.BOT_USER.username)):
        if now > mktime((user.last_login + timedelta(hours=24)).timetuple()):
            Logger.debug(
                unicode('user %s logged out, set alarm to True' % (user.username)),
                extra={
                    'user': user.username,
                }
            )
            user.ticketUser.filter(where).update(alarm=True)
        else:
            Logger.debug(
                str('user %s logged in, set alarm to False' % (user.username)),
                extra={
                    'user': user.username,
                }
            )
            user.ticketUser.filter(where).update(alarm=False)


def update_waiting():
    """
        Update waiting answer tickets
    """
    now = int(time())
    for ticket in Ticket.objects.filter(status=WAITING):
        try:
            if now > int(mktime(ticket.snoozeStart.timetuple()) + ticket.snoozeDuration):
                Logger.debug(
                    unicode('Updating status for ticket %s ' % (ticket.id)),
                    extra={
                        'ticket': ticket.id,
                    }
                )
                _check_auto_unassignation(ticket)
                common.set_ticket_status(ticket, ALARM, reset_snooze=True)

        except (AttributeError, ValueError) as ex:
            Logger.debug(unicode('Error while updating ticket %d : %s' % (ticket.id, ex)))


def _check_auto_unassignation(ticket):

    history = ticket.ticketHistory.filter(
        actionType='ChangeStatus'
    ).order_by('-date').values_list(
        'ticketStatus',
        flat=True
    )[:3]

    try:
        unassigned_on_multiple_alarm = ticket.treatedBy.operator.role.modelsAuthorizations['ticket']['unassignedOnMultipleAlarm']
        if unassigned_on_multiple_alarm and len(history) == 3 and all([STATUS_SEQUENCE[i] == history[i] for i in xrange(3)]):
            database.log_action_on_ticket(
                ticket=ticket,
                action='change_treatedby',
                previous_value=ticket.treatedBy
            )
            database.log_action_on_ticket(
                ticket=ticket,
                action='update_property',
                property='escalated',
                previous_value=ticket.escalated,
                new_value=True,
            )
            ticket.treatedBy = None
            ticket.escalated = True
            Logger.debug(unicode(
                'Unassigning ticket %d because of operator role configuration' % (ticket.id)
            ))
    except (AttributeError, KeyError, ObjectDoesNotExist, ValueError):
        pass


def update_paused():
    """
        Update paused tickets
    """
    now = int(time())
    for ticket in Ticket.objects.filter(status=PAUSED):
        try:
            if now > int(mktime(ticket.pauseStart.timetuple()) + ticket.pauseDuration):
                Logger.debug(
                    str('Updating status for ticket %s ' % (ticket.id)),
                    extra={
                        'ticket': ticket.id,
                    }
                )
                if ticket.previousStatus == WAITING and ticket.snoozeDuration and ticket.snoozeStart:
                    ticket.snoozeDuration = ticket.snoozeDuration + (datetime.now() - ticket.pauseStart).seconds

                common.set_ticket_status(ticket, ticket.previousStatus)
                ticket.pauseStart = None
                ticket.pauseDuration = None
                ticket.save()

        except (AttributeError, ValueError) as ex:
            Logger.debug(unicode('Error while updating ticket %d : %s' % (ticket.id, ex)))
