import copy
import json
import logging
import re
from typing import List, Optional

import celpy
import celpy.c7nlib
import celpy.celparser
import celpy.celtypes
import celpy.evaluation
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session

from keep.api.bl.incidents_bl import IncidentBl
from keep.api.core.db import (
    assign_alert_to_incident,
    create_incident_for_grouping_rule,
    enrich_incidents_with_alerts,
    get_incident_for_grouping_rule,
)
from keep.api.core.db import get_rules as get_rules_db
from keep.api.core.db import is_all_alerts_in_status
from keep.api.core.dependencies import get_pusher_client
from keep.api.models.alert import AlertDto, AlertSeverity, AlertStatus
from keep.api.models.db.alert import Incident
from keep.api.models.db.rule import Rule
from keep.api.models.incident import IncidentDto
from keep.api.utils.cel_utils import preprocess_cel_expression
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts

# Shahar: this is performance enhancment https://github.com/cloud-custodian/cel-python/issues/68


celpy.evaluation.Referent.__repr__ = lambda self: ""
celpy.evaluation.NameContainer.__repr__ = lambda self: ""
celpy.Activation.__repr__ = lambda self: ""
celpy.Activation.__str__ = lambda self: ""
celpy.celtypes.MapType.__repr__ = lambda self: ""
celpy.celtypes.DoubleType.__repr__ = lambda self: ""
celpy.celtypes.BytesType.__repr__ = lambda self: ""
celpy.celtypes.IntType.__repr__ = lambda self: ""
celpy.celtypes.UintType.__repr__ = lambda self: ""
celpy.celtypes.ListType.__repr__ = lambda self: ""
celpy.celtypes.StringType.__repr__ = lambda self: ""
celpy.celtypes.TimestampType.__repr__ = lambda self: ""
celpy.c7nlib.C7NContext.__repr__ = lambda self: ""
celpy.celparser.Tree.__repr__ = lambda self: ""


class RulesEngine:
    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id
        self.logger = logging.getLogger(__name__)
        self.env = celpy.Environment()

    def run_rules(
        self, events: list[AlertDto], session: Optional[Session] = None
    ) -> list[IncidentDto]:
        """
        Evaluate the rules on the events and create incidents if needed
        Args:
            events: list of events
            session: db session
        """
        self.logger.info("Running CEL rules")
        cel_incidents = self._run_cel_rules(events, session)
        self.logger.info("CEL rules ran successfully")

        return cel_incidents

    def _run_cel_rules(
        self, events: list[AlertDto], session: Optional[Session] = None
    ) -> list[IncidentDto]:
        """
        Evaluate the rules on the events and create incidents if needed
        Args:
            events: list of events
            session: db session
        """
        self.logger.info("Running rules")
        rules = get_rules_db(tenant_id=self.tenant_id)

        incidents_dto = {}
        for rule in rules:
            self.logger.info(f"Evaluating rule {rule.name}")
            for event in events:
                self.logger.info(
                    f"Checking if rule {rule.name} apply to event {event.id}"
                )
                try:
                    matched_rules = self._check_if_rule_apply(rule, event)
                except ValueError as e:
                    if "Invalid name" in str(e):
                        self.logger.warning(
                            f"{str(e)} in the CEL expression {rule.definition_cel} for alert {event.id}. This might mean there's a blank space in the field name",
                            extra={"alert_id": event.id, "payload": event.dict()},
                        )
                        continue
                except Exception:
                    self.logger.exception(
                        f"Failed to evaluate rule {rule.name} on event {event.id}",
                        extra={
                            "rule": rule.dict(),
                            "event": event.dict(),
                        },
                    )
                    continue

                if matched_rules:
                    self.logger.info(
                        f"Rule {rule.name} on event {event.id} is relevant"
                    )

                    rule_fingerprints = self._calc_rule_fingerprint(event, rule)

                    for rule_fingerprint in rule_fingerprints:
                        incident, send_created_event = self._get_or_create_incident(
                            rule=rule,
                            rule_fingerprint=",".join(rule_fingerprint),
                            session=session,
                            event=event,
                        )
                        if incident:
                            incident = assign_alert_to_incident(
                                fingerprint=event.fingerprint,
                                incident=incident,
                                tenant_id=self.tenant_id,
                                session=session,
                            )

                            if not incident.is_visible:

                                self.logger.info(
                                    f"No existing incidents for rule {rule.name}. Checking incident creation conditions"
                                )

                                rule_groups = self._extract_subrules(
                                    rule.definition_cel
                                )
                                firing_count = sum(
                                    [
                                        alert.event.get("unresolvedCounter", 1)
                                        for alert in incident.alerts
                                    ]
                                )
                                alerts_count = max(incident.alerts_count, firing_count)
                                if alerts_count >= rule.threshold:
                                    if not rule.require_approve:
                                        if rule.create_on == "any" or (
                                            rule.create_on == "all"
                                            and len(rule_groups) == len(matched_rules)
                                        ):
                                            self.logger.info(
                                                "Single event is enough, so creating incident"
                                            )
                                            incident.is_visible = True
                                        elif rule.create_on == "all":
                                            incident = self._process_event_for_history_based_rule(
                                                incident, rule, session
                                            )

                                send_created_event = incident.is_visible

                            # If we try to access incident.id inside except block, it will try to refresh
                            # instance and raises PendingRollback error
                            incident_id = incident.id

                            # Incident instance might change till this moment (set visible for example),
                            # so we need to commit changes
                            # Otherwise sqlalchemy might try to do this in unpredictable moment
                            for attempt in range(3):
                                try:
                                    # Explicitly add incident, but it most likely already there, since it was loaded in
                                    # same session
                                    session.add(incident)
                                    session.commit()
                                    break
                                except StaleDataError as ex:
                                    if "expected to update" in ex.args[0]:
                                        self.logger.warning(
                                            f"Race condition met while updating incident `{incident_id}`, retry #{attempt}"
                                        )
                                        session.rollback()
                                        continue
                                    else:
                                        raise

                            incident = IncidentBl(
                                self.tenant_id, session
                            ).resolve_incident_if_require(incident)

                            incident_dto = IncidentDto.from_db_incident(incident)
                            if send_created_event:
                                RulesEngine.send_workflow_event(
                                    self.tenant_id, session, incident_dto, "created"
                                )
                            elif incident.is_visible:
                                RulesEngine.send_workflow_event(
                                    self.tenant_id, session, incident_dto, "updated"
                                )

                            incidents_dto[incident.id] = incident_dto

                else:
                    self.logger.info(
                        f"Rule {rule.name} on event {event.id} is not relevant"
                    )

        self.logger.info("Rules ran successfully")
        # if we don't have any updated groups, we don't need to create any alerts
        if not incidents_dto:
            return []

        self.logger.info(f"Rules ran, {len(incidents_dto)} incidents created")

        return list(incidents_dto.values())

    def get_value_from_event(self, event: AlertDto, var: str) -> str:
        """
        Extract value from event based on template variable
        e.g., alert.labels.host -> event['labels']['host']
            alert.service -> event['service']
        """
        # Remove 'alert.' prefix
        path = var.replace("alert.", "").split(".")

        current = event.dict()  # Convert to dict for easier access
        try:
            for part in path:
                part = part.strip()
                current = current.get(part)
            return str(current) if current is not None else "N/A"
        except (KeyError, AttributeError):
            return "N/A"

    def get_vaiables(self, incident_name_template):
        regex = r"\{\{\s*([^}]+)\s*\}\}"
        return re.findall(regex, incident_name_template)

    def _get_or_create_incident(
        self, rule: Rule, rule_fingerprint, session, event
    ) -> (Optional[Incident], bool):

        existed_incident, expired = get_incident_for_grouping_rule(
            self.tenant_id,
            rule,
            rule_fingerprint,
            session=session,
        )

        if existed_incident and not expired and rule.incident_prefix:
            if rule.incident_prefix not in existed_incident.user_generated_name:
                existed_incident.user_generated_name = f"{rule.incident_prefix}-{existed_incident.running_number} - {existed_incident.user_generated_name}"
                self.logger.info(
                    "Incident name updated with prefix",
                )

        # if not incident name template, return the incident
        if existed_incident and not expired and not rule.incident_name_template:
            return existed_incident, False
        # if incident name template, merge
        elif existed_incident and not expired:
            incident_name = copy.copy(rule.incident_name_template)
            current_name = existed_incident.user_generated_name
            self.logger.info(
                "Updating the incident name based on the new event",
                extra={
                    "incident_id": existed_incident.id,
                    "incident_name": current_name,
                },
            )
            alerts = existed_incident.alerts
            variables = self.get_vaiables(rule.incident_name_template)
            values = set()
            for var in variables:
                var_to_replace = ""
                alerts_dtos = convert_db_alerts_to_dto_alerts(alerts)
                for alert in alerts_dtos:
                    value = self.get_value_from_event(alert, var)
                    # don't add twice the same value
                    if value not in values:
                        var_to_replace += value + ","
                        values.add(value)
                this_event_val = self.get_value_from_event(event, var)
                if this_event_val not in values:
                    var_to_replace += this_event_val
                pattern = r"\{\{\s*" + re.escape(var) + r"\s*\}\}"
                # it happens when the last value is already in the incident name so its skipped
                if var_to_replace.endswith(","):
                    var_to_replace = var_to_replace[:-1]
                # update the incident name template
                # note that it will be commited later, when the incident is commited
                incident_name = re.sub(pattern, var_to_replace, incident_name)
            # we are done
            if existed_incident.user_generated_name != incident_name:
                existed_incident.user_generated_name = incident_name
                self.logger.info(
                    "Incident name updated",
                    extra={
                        "incident_id": existed_incident.id,
                        "old_incident_name": current_name,
                        "new_incident_name": existed_incident.user_generated_name,
                    },
                )
            return existed_incident, False

        # else, this is the first time
        # Starting new incident ONLY if alert is firing
        # https://github.com/keephq/keep/issues/3418
        if event.status == AlertStatus.FIRING.value:
            if rule.incident_name_template:
                incident_name = copy.copy(rule.incident_name_template)
                variables = self.get_vaiables(rule.incident_name_template)
                if not variables:
                    self.logger.warning(
                        f"Failed to fetch the appropriate labels from the event {event.id} and rule {rule.name}"
                    )
                    incident_name = None
                for var in variables:
                    value = self.get_value_from_event(event, var)
                    pattern = r"\{\{\s*" + re.escape(var) + r"\s*\}\}"
                    incident_name = re.sub(pattern, value, incident_name)
            else:
                incident_name = None

            if rule.multi_level:
                incident_name = (
                    f"{rule_fingerprint} - {rule.name}"
                    if not incident_name
                    else f"{rule_fingerprint} - {incident_name}"
                )

            incident = create_incident_for_grouping_rule(
                tenant_id=self.tenant_id,
                rule=rule,
                rule_fingerprint=rule_fingerprint,
                session=session,
                incident_name=incident_name,
                past_incident=existed_incident,
                assignee=rule.assignee,
            )
            return incident, True
        return None, False

    def _process_event_for_history_based_rule(
        self, incident: Incident, rule: Rule, session: Session
    ) -> Incident:
        self.logger.info("Multiple events required for the incident to start")

        enrich_incidents_with_alerts(
            tenant_id=self.tenant_id,
            incidents=[incident],
            session=session,
        )

        fingerprints = [alert.fingerprint for alert in incident.alerts]

        is_all_conditions_met = False

        all_sub_rules = set(self._extract_subrules(rule.definition_cel))
        matched_sub_rules = set()

        for alert in incident.alerts:
            matched_sub_rules = matched_sub_rules.union(
                self._check_if_rule_apply(rule, AlertDto(**alert.event))
            )
            if all_sub_rules == matched_sub_rules:
                is_all_conditions_met = True
                break

        if is_all_conditions_met:
            all_alerts_firing = is_all_alerts_in_status(
                fingerprints=fingerprints, status=AlertStatus.FIRING, session=session
            )
            if all_alerts_firing:
                incident.is_visible = True
                session.add(incident)
                session.commit()

        return incident

    @staticmethod
    def _extract_subrules(expression):
        # CEL rules looks like '(source == "sentry") || (source == "grafana" && severity == "critical")'
        # and we need to extract the subrules
        sub_rules = expression.split(") || (")
        if len(sub_rules) == 1:
            return sub_rules
        # the first and the last rules will have a ( or ) at the beginning or the end
        # e.g. for the example of:
        #           (source == "sentry") && (source == "grafana" && severity == "critical")
        # than sub_rules[0] will be (source == "sentry" and sub_rules[-1] will be source == "grafana" && severity == "critical")
        # so we need to remove the first and last character
        sub_rules[0] = sub_rules[0][1:]
        sub_rules[-1] = sub_rules[-1][:-1]
        return sub_rules

    @staticmethod
    def sanitize_cel_payload(payload):
        """
        Remove keys containing forbidden characters from payload and return warnings.
        Returns tuple of (sanitized_payload, warnings)
        """
        forbidden_starts = [
            "@",
            "-",
            "$",
            "#",
            " ",
            ":",
            ".",
            "/",
            "\\",
            "*",
            "&",
            "^",
            "%",
            "!",
        ]
        logger = logging.getLogger(__name__)

        def _sanitize_dict(d):
            result = {}
            for k, v in d.items():
                if k[0] in forbidden_starts:  # Only check first character
                    logger.warning(
                        f"Removed key '{k}' starting with forbidden character '{k[0]}'"
                    )
                    continue

                if isinstance(v, dict):
                    result[k] = _sanitize_dict(v)
                elif isinstance(v, list):
                    result[k] = [
                        _sanitize_dict(i) if isinstance(i, dict) else i for i in v
                    ]
                else:
                    result[k] = v
            return result

        sanitized = _sanitize_dict(payload)
        return sanitized

    def _check_if_rule_apply(self, rule: Rule, event: AlertDto) -> List[str]:
        """
        Evaluates if a rule applies to an event using CEL. Handles type coercion for ==/!= between int and str.
        """
        sub_rules = self._extract_subrules(rule.definition_cel)
        payload = event.dict()
        # workaround since source is a list
        # todo: fix this in the future
        payload["source"] = payload["source"][0]
        payload = RulesEngine.sanitize_cel_payload(payload)

        # what we do here is to compile the CEL rule and evaluate it
        #   https://github.com/cloud-custodian/cel-python
        #   https://github.com/google/cel-spec
        sub_rules_matched = []
        for sub_rule in sub_rules:
            # Shahar: rules such as "(source != null)" causing an exception:
            #           celpy.evaluation.CELEvalError: ("found no matching overload for 'relation_ne' applied to
            #           '(<class 'celpy.celtypes.StringType'>, <class 'NoneType'>)'", <class 'TypeError'>,
            #            ("no such overload:  <class 'celpy.celtypes.StringType'> != None <class 'NoneType'>",))
            #          So we need to replace "null" with ""
            #
            #          TODO: it works for strings now, but we need to add support on list/dict when needed
            if "null" in sub_rule:
                sub_rule = sub_rule.replace("null", '""')
            ast = self.env.compile(sub_rule)
            prgm = self.env.program(ast)
            activation = celpy.json_to_cel(json.loads(json.dumps(payload, default=str)))
            try:
                r = prgm.evaluate(activation)
            except celpy.evaluation.CELEvalError as e:
                # this is ok, it means that the subrule is not relevant for this event
                if "no such member" in str(e):
                    continue
                # unknown
                # --- Fix for https://github.com/keephq/keep/issues/5107 ---
                if "no such overload" in str(e) or "found no matching overload" in str(
                    e
                ):
                    try:
                        coerced = self._coerce_eq_type_error(
                            sub_rule, prgm, activation, event
                        )
                        if coerced:
                            sub_rules_matched.append(sub_rule)
                            continue
                    except Exception:
                        pass
                raise
            if r:
                sub_rules_matched.append(sub_rule)
        # no subrules matched
        return sub_rules_matched

    def _coerce_eq_type_error(self, cel, prgm, activation, alert):
        """
        Helper for type coercion fallback for ==/!= between int and str in CEL.
        Fixes https://github.com/keephq/keep/issues/5107
        """
        import re

        m = re.match(r"([a-zA-Z0-9_\.]+)\s*([!=]=)\s*(.+)", cel)
        if not m:
            return False
        left, op, right = m.groups()
        left = left.strip()
        right = (
            right.strip().strip('"')
            if right.strip().startswith('"') and right.strip().endswith('"')
            else right.strip()
        )
        try:

            def get_nested(d, path):
                for part in path.split("."):
                    if isinstance(d, dict):
                        d = d.get(part)
                    else:
                        return None
                return d

            left_val = get_nested(activation, left)
            try:
                right_val = int(right)
            except Exception:
                try:
                    right_val = float(right)
                except Exception:
                    right_val = right
            # If one is str and the other is int/float, compare as str
            if (isinstance(left_val, (int, float)) and isinstance(right_val, str)) or (
                isinstance(left_val, str) and isinstance(right_val, (int, float))
            ):
                if op == "==":
                    return str(left_val) == str(right_val)
                else:
                    return str(left_val) != str(right_val)
            # Also handle both as str for robustness
            if op == "==":
                return str(left_val) == str(right_val)
            else:
                return str(left_val) != str(right_val)
        except Exception:
            pass
        return False

    def _calc_rule_fingerprint(self, event: AlertDto, rule: Rule) -> list[list[str]]:
        # extract all the grouping criteria from the event
        # e.g. if the grouping criteria is ["event.labels.queue", "event.labels.cluster"]
        #     and the event is:
        #    {
        #      "labels": {
        #        "queue": "queue1",
        #        "cluster": "cluster1",
        #        "foo": "bar"
        #      }
        #    }
        # than the rule_fingerprint will be "[queue1,cluster1]"
        # if the rule is multi_level, the rule_fingerprint will be "[queue1,cluster1]" and "[queue2,cluster2]" and more than 1 incident will be created

        # note: rule_fingerprint is not a unique id, since different rules can lead to the same rule_fingerprint
        #       hence, the actual fingerprint is composed of the rule_fingerprint and the incident id
        event_payload = event.dict()
        grouping_criteria = rule.grouping_criteria or []

        if not rule.multi_level:
            rule_fingerprints = []
            for criteria in grouping_criteria:
                # we need to extract the value from the event
                # e.g. if the criteria is "event.labels.queue"
                # than we need to extract the value of event["labels"]["queue"]
                criteria_parts = criteria.split(".")
                value = event_payload
                for part in criteria_parts:
                    value = value.get(part)
                if isinstance(value, list):
                    value = ",".join(value)

                rule_fingerprints.append(value)
            # if, for example, the event should have labels.X but it doesn't,
            # than we will have None in the rule_fingerprint
            if not rule_fingerprints:
                self.logger.warning(
                    f"Failed to calculate rule fingerprint for event {event.id} and rule {rule.name}",
                    extra={
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "tenant_id": self.tenant_id,
                    },
                )
                return [["none"]]
            # if any of the values is None, we will return "none"
            if any([fingerprint is None for fingerprint in rule_fingerprints]):
                self.logger.warning(
                    f"Failed to fetch the appropriate labels from the event {event.id} and rule {rule.name}",
                    extra={
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "tenant_id": self.tenant_id,
                    },
                )
                return [["none"]]
            return [rule_fingerprints]
        else:
            fingerprints = set()
            # the idea is pretty simple but implementation is a bit hacky for now
            # we expect the grouping criteria to be a dict with the key being the property name
            # for example: {"customers": {"1": {"name": "John", "age": 30}, "2": {"name": "Jane", "age": 25}}}
            # and we want to group by the "name" property
            # so we will get ["John", "Jane"] and 2 incidents will be created: one for "John" and one for "Jane" with same alerts.
            if not grouping_criteria:
                self.logger.warning(
                    "wtf? no grouping criteria for multi_level rule",
                    extra={
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "tenant_id": self.tenant_id,
                    },
                )
                return [["none"]]
            # @tb: this is a known limitation for now, we only accept 1 grouping criteria for multi_level rule
            criteria = grouping_criteria[0]
            criteria_parts = criteria.split(".")
            for part in criteria_parts:
                value = event_payload
                for part in criteria_parts:
                    value = value.get(part)
                if not isinstance(value, dict):
                    self.logger.warning(
                        "multi level rule grouping criteria is not a dict",
                        extra={
                            "rule_id": rule.id,
                            "rule_name": rule.name,
                            "tenant_id": self.tenant_id,
                        },
                    )
                    return [["none"]]
                for key in value.keys():
                    fingerprints.add(value[key].get(rule.multi_level_property_name))
                return [[key] for key in fingerprints]
        return [["none"]]

    @staticmethod
    def get_alerts_activation(alerts: list[AlertDto]):
        activations = []
        for alert in alerts:
            payload = alert.dict()
            # TODO: workaround since source is a list
            #       should be fixed in the future
            payload["source"] = ",".join(payload["source"])
            # payload severity could be the severity itself or the order of the severity, cast it to the order
            if isinstance(payload["severity"], str):
                payload["severity"] = AlertSeverity(payload["severity"].lower()).order

            # sanitize the payload
            payload = RulesEngine.sanitize_cel_payload(payload)
            activation = celpy.json_to_cel(json.loads(json.dumps(payload, default=str)))
            activations.append(activation)
        return activations

    def filter_alerts(
        self, alerts: list[AlertDto], cel: str, alerts_activation: list = None
    ):
        """This function filters alerts according to a CEL

        Args:
            alerts (list[AlertDto]): list of alerts
            cel (str): CEL expression

        Returns:
            list[AlertDto]: list of alerts that are related to the cel
        """
        logger = logging.getLogger(__name__)
        # if the cel is empty, return all the alerts
        if cel == "":
            return alerts
        # if the cel is empty, return all the alerts
        if not cel:
            logger.debug("No CEL expression provided")
            return alerts
        # preprocess the cel expression
        cel = preprocess_cel_expression(cel)
        ast = self.env.compile(cel)
        prgm = self.env.program(ast)
        filtered_alerts = []

        for i, alert in enumerate(alerts):
            if alerts_activation:
                activation = alerts_activation[i]
            else:
                activation = self.get_alerts_activation([alert])[0]
            try:
                r = prgm.evaluate(activation)
            except ValueError as e:
                if "Invalid name" in str(e):
                    logger.warning(
                        f"{str(e)} in the CEL expression {cel} for alert {alert.id}. This might mean there's a blank space in the field name",
                        extra={"alert_id": alert.id, "payload": alert.dict()},
                    )
                    continue
            except celpy.evaluation.CELEvalError as e:
                # this is ok, it means that the subrule is not relevant for this event
                if "no such member" in str(e):
                    continue
                # unknown
                elif "no such overload" in str(
                    e
                ) or "found no matching overload" in str(e):
                    # Try type coercion for == and !=
                    try:
                        coerced = self._coerce_eq_type_error(
                            cel, prgm, activation, alert
                        )
                        if coerced:
                            filtered_alerts.append(alert)
                            continue
                    except Exception:
                        pass
                    logger.debug(
                        f"Type mismtach between operator and operand in the CEL expression {cel} for alert {alert.id}"
                    )
                    continue
                logger.warning(
                    f"Failed to evaluate the CEL expression {cel} for alert {alert.id} - {e}"
                )
                continue
            except Exception:
                logger.exception(
                    f"Failed to evaluate the CEL expression {cel} for alert {alert.id}"
                )
                continue
            if r:
                filtered_alerts.append(alert)

        return filtered_alerts

    @staticmethod
    def send_workflow_event(
        tenant_id: str, session: Session, incident_dto: IncidentDto, action: str
    ):
        logger = logging.getLogger(__name__)
        logger.info(f"Sending workflow event {action} for incident {incident_dto.id}")
        pusher_client = get_pusher_client()
        incident_bl = IncidentBl(tenant_id, session, pusher_client)

        incident_bl.send_workflow_event(incident_dto, action)
        incident_bl.update_client_on_incident_change(incident_dto.id)
        logger.info(f"Workflow event {action} for incident {incident_dto.id} sent")
