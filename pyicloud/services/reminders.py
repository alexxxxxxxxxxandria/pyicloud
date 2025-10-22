"""Reminders service."""

import re
import json
import logging
from abc import ABC
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator, Optional, Union
from urllib.parse import urlencode

from requests import Response

from pyicloud.const import CONTENT_TYPE, CONTENT_TYPE_TEXT
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudServiceNotActivatedException,
)
from pyicloud.services.base import BaseService
from pyicloud.session import PyiCloudSession


_LOGGER: logging.Logger = logging.getLogger(__name__)

# The primary zone for the user's reminders
PRIMARY_ZONE: dict[str, str] = {
    "zoneName": "Reminders",
    "zoneType": "REGULAR_CUSTOM_ZONE",
}

## https://stackoverflow.com/a/1176023
snake_case_pattern = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def to_snake_case(data: str) -> str:
    return (
        snake_case_pattern.sub("_", data).lower().replace("_i_ds", "_ids")
    )  ## Assume "_i_ds" is a regex typo for "_ids"


def unmarshall(data: dict[str, Any]) -> dict[str, Any]:
    """Unmarshall the data received from iCloud."""

    def timestamp_milliseconds_to_datetime(ts: int) -> Union[datetime, None]:
        """Convert a timestamp in milliseconds to a datetime object."""
        try:
            return datetime.fromtimestamp(ts / 1000.0, timezone.utc)
        except ValueError:
            return None

    def timestamp_seconds_to_datetime(ts: int) -> Union[datetime, None]:
        try:
            return datetime.fromtimestamp(ts, timezone.utc)
        except ValueError:
            return None

    def unmarshall_activity(data: Union[dict[str, Any], bool]) -> dict[str, Any]:
        """Unmarshall Created/Modified/Deleted items."""

        result: dict[str, Any] = {
            "timestamp": None,
            "user_id": None,
            "device_id": None,
        }

        if isinstance(data, bool):
            if data is True:
                raise AssertionError(
                    "Activity data should be a dict or boolean False. Received True instead."
                )
        elif not isinstance(data, dict):
            raise ValueError(
                f"Invalid activity data (expected a dict or boolean False): {data}"
            )

        if not data:
            return result

        if "timestamp" not in data:
            raise ValueError(f"Invalid activity data (missing 'timestamp'): {data}")
        else:
            result["timestamp"] = timestamp_milliseconds_to_datetime(data["timestamp"])

        if result["timestamp"] is None:
            raise ValueError(f"Invalid timestamp {data['timestamp']} in data {data}")

        result["user_id"] = data.get("userRecordName", None)
        result["device_id"] = data.get("deviceID", None)

        return result

    result: dict[str, Any] = {}
    for data_key, data_value in data.items():
        if data_key == "recordName":
            result["record_id"] = data_value
        elif data_key in ["created", "deleted", "modified"]:
            parsed = unmarshall_activity(data_value)
            result[f"{to_snake_case(data_key)}_date"] = parsed["timestamp"]
            result[f"{to_snake_case(data_key)}_by_user"] = parsed["user_id"]
            result[f"{to_snake_case(data_key)}_by_device"] = parsed["device_id"]
        elif data_key == "expirationTime":
            result["expiration_date"] = timestamp_seconds_to_datetime(data_value)
        elif data_key == "fields":
            ## Map the fields to from objects to primitive types
            fields: dict[str, Any] = {}
            for field_name, field_value in data_value.items():
                field_name = to_snake_case(field_name)
                if field_value["type"] == "STRING":
                    fields[field_name] = field_value["value"]
                elif field_value["type"] == "INT64":
                    fields[field_name] = int(field_value["value"])
                elif field_value["type"] == "BOOLEAN":
                    fields[field_name] = bool(field_value["value"])
                elif field_value["type"] == "TIMESTAMP":
                    fields[field_name] = timestamp_milliseconds_to_datetime(
                        field_value["value"]
                    )
                elif field_value["type"] == "ASSETID":
                    fields[field_name] = field_value["value"]
                else:
                    _LOGGER.warning(
                        "Unknown field type %s for field %s in record %s",
                        field_value["type"],
                        field_name,
                        result.get("record_id", "unknown"),
                    )
                    fields[field_name] = field_value

                if field_name in ["color", "reminder_ids", "resolution_token_map"]:
                    try:
                        fields[field_name] = json.loads(field_value["value"])
                    except json.JSONDecodeError:
                        _LOGGER.warning(
                            "Failed to parse field %s with value %s in record %s",
                            field_name,
                            field_value["value"],
                            result.get("record_id", "unknown"),
                        )
                        fields[field_name] = {}
                elif field_name in [
                    "deleted",
                    "imported",
                    "is_group",
                    "is_linked_to_account",
                    "should_categorize_grocery_items",
                ]:
                    ## These are boolean fields
                    fields[field_name] = fields[field_name] == 1
                elif field_name in ["sorting_style"]:
                    fields[field_name] = (
                        SortingStyleEnum.from_string(
                            data["fields"].get(field_name, "manual")
                        ),
                    )

            if "name" not in fields or not fields["name"]:
                _LOGGER.warning(
                    "Field 'name' is missing or empty in record %s. Using 'Unknown' as default.",
                    result.get("record_id", "unknown"),
                )
                fields["name"] = "Unknown"
            result["fields"] = fields
        elif data_key == "share":
            ## This one is explicitly called out because it's an object
            result[to_snake_case(data_key)] = data_value
        elif (
            isinstance(data_value, str)
            or isinstance(data_value, int)
            or isinstance(data_value, bool)
        ):
            result[to_snake_case(data_key)] = data_value
        else:
            _LOGGER.warning(
                "Field %s has complex type %s but no special handling. record_id: %s. value: %s",
                data_key,
                type(data_value),
                result.get("record_id", "unknown"),
                data_value,
            )
            result[to_snake_case(data_key)] = data_value

    return result


class SortingStyleEnum(str, Enum):
    """Sorting styles for reminders."""

    MANUAL = "manual"
    DISPLAY_DATE_ASCENDING = "displayDate_asc"
    DISPLAY_DATE_DESCENDING = "displayDate_desc"
    TITLE_ASCENDING = "title_asc"
    TITLE_DESCENDING = "title_desc"

    @staticmethod
    def from_string(value: str) -> "SortingStyleEnum":
        """Convert a string to a SortingStyleEnum."""
        if value == "manual":
            return SortingStyleEnum.MANUAL
        elif value == "displayDate_asc":
            return SortingStyleEnum.DISPLAY_DATE_ASCENDING
        elif value == "displayDate_desc":
            return SortingStyleEnum.DISPLAY_DATE_DESCENDING
        elif value == "title_asc":
            return SortingStyleEnum.TITLE_ASCENDING
        elif value == "title_desc":
            return SortingStyleEnum.TITLE_DESCENDING
        else:
            raise ValueError(f"Unknown sorting style: {value}")

    # def __str__(self) -> str:
    #     """Return the string representation of the sorting style."""
    #     return self.value


class BaseReminder:
    """Represents a reminder."""

    def __init__(
        self,
        service: "RemindersService",
        list_id: str,
        title: str,
        notes: str,
        is_all_day: bool,
        is_completed: bool,
        is_deleted: bool,
        is_flagged: bool,
        is_imported: bool,
        created_date: datetime,
        created_by_user: str,
        created_by_device: str,
        modified_date: datetime,
        modified_by_user: str,
        modified_by_device: str,
        deleted_date: Optional[datetime] = None,
        deleted_by_user: Optional[str] = None,
        deleted_by_device: Optional[str] = None,
    ) -> None:
        self._service: RemindersService = service
        self._list_id: str = list_id
        self._title: str = title
        self._notes: str = notes
        self._is_all_day: bool = is_all_day
        self._is_completed: bool = is_completed
        self._is_deleted: bool = is_deleted
        self._is_flagged: bool = is_flagged
        self._is_imported: bool = is_imported
        self._created_date: datetime = created_date
        self._created_by_user: str = created_by_user
        self._created_by_device: str = created_by_device
        self._modified_date: datetime = modified_date
        self._modified_by_user: str = modified_by_user
        self._modified_by_device: str = modified_by_device
        self._deleted_date: Optional[datetime] = deleted_date
        self._deleted_by_user: Optional[str] = deleted_by_user
        self._deleted_by_device: Optional[str] = deleted_by_device

    @property
    def title(self) -> str:
        """Returns the title of the reminder."""
        return self._title

    @property
    def notes(self) -> str:
        """Returns the description of the reminder."""
        return self._notes


class BaseRemindersList(ABC):
    """Represents a Reminders list"""

    def __init__(
        self,
        service: "RemindersService",
        list_id: str,
        created_date: datetime,
        created_by_user: str,
        created_by_device: str,
        modified_date: datetime,
        modified_by_user: str,
        modified_by_device: str,
        deleted_date: Optional[datetime] = None,
        deleted_by_user: Optional[str] = None,
        deleted_by_device: Optional[str] = None,
        badge_emblem: Optional[str] = None,
        chain_private_key: Optional[str] = None,
        chain_protection_info: Optional[dict[str, Any]] = None,
        color: Optional[str] = None,
        count: Optional[int] = None,
        deleted: Optional[bool] = None,
        displayed_hostname: Optional[str] = None,
        expiration_date: Optional[datetime] = None,
        grocery_local_corrections_as_data: Optional[dict[str, Any]] = None,
        grocery_local_corrections_checksum: Optional[str] = None,
        grocery_locale_id: Optional[str] = None,
        imported: Optional[bool] = None,
        imported_date: Optional[datetime] = None,
        is_group: Optional[bool] = None,
        is_linked_to_account: Optional[bool] = None,
        list_name: Optional[str] = None,
        memberships_of_reminders_in_sections_as_data: Optional[dict[str, Any]] = None,
        memberships_of_reminders_in_sections_checksum: Optional[str] = None,
        parent_list: Optional["BaseRemindersList"] = None,
        pinned_date: Optional[datetime] = None,
        plugin_fields: Optional[Any] = None,
        record_change_tag: Optional[str] = None,
        reminder_ids_asset: Optional[dict[str, Any]] = None,
        reminder_ids: Optional[list[str]] = None,
        resolution_token_map: Optional[dict[str, Any]] = None,
        section_ids_ordering_as_data: Optional[dict[str, Any]] = None,
        share: Optional[dict[str, Any]] = None,
        short_guid: Optional[str] = None,
        should_categorize_grocery_items: Optional[bool] = None,
        sorting_style: Optional[SortingStyleEnum] = None,
        stable_url: Optional[str] = None,
    ) -> None:
        self.service: RemindersService = service
        self._reminders: Union[dict[str, BaseReminder], None] = None

        ## Core Attributes
        self.list_id: str = list_id
        self.name: str = list_name

        ## Fields
        self.count: int = count
        self.deleted: bool = deleted
        self.imported: bool = imported
        self.is_group: bool = is_group
        self.is_linked_to_account: bool = is_linked_to_account
        self.record_change_tag: str = record_change_tag
        self.reminder_ids: list[str] = reminder_ids
        self.resolution_token_map: dict[str, Any] = resolution_token_map
        self.sorting_style: SortingStyleEnum = sorting_style

        ## Additional Metadata
        self.parent_list: Optional["BaseRemindersList"] = parent_list
        self.plugin_fields: Any = plugin_fields
        self.created_date: datetime = created_date
        self.created_by_user: str = created_by_user
        self.created_by_device: str = created_by_device
        self.modified_date: datetime = modified_date
        self.modified_by_user: str = modified_by_user
        self.modified_by_device: str = modified_by_device
        self.deleted_date: Optional[datetime] = deleted_date
        self.deleted_by_user: Optional[str] = deleted_by_user
        self.deleted_by_device: Optional[str] = deleted_by_device

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.name}' ({self.list_id})>"

    ## Methods

    @staticmethod
    def from_record(
        service: "RemindersService",
        record: dict[str, Any],
    ) -> "BaseRemindersList":
        """Create the correct type of RemindersList from an API record."""
        if record["recordType"] != "List":
            raise PyiCloudAPIResponseException(
                "Record is not a List", repr({"recordType": record["recordType"]})
            )

        data = unmarshall(record)
        if not data:
            raise PyiCloudAPIResponseException(
                "Failed to unmarshall record", repr(record)
            )

        data["list_id"] = data.pop("record_id")
        data["list_name"] = data["fields"].pop("name")
        del data["record_type"]
        for field_name, field_value in data["fields"].items():
            if field_name in data and field_name not in ["name"]:
                raise KeyError(
                    f"Field '{field_name}' already exists in data: {json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)}"
                )
            data[field_name] = field_value
        del data["fields"]
        del data["zone_id"]

        try:
            ## Make list_name only Alphanumeric and underscores
            # file_name = re.sub(r"[^a-zA-Z0-9_]", "_", data["list_name"])

            # with open(f"/out/{file_name}.json", "w", encoding="utf-8") as f:
            #     f.write(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, default=str))
            if "share" in data:
                return SharedRemindersList.from_data(service, data)
            elif "should_categorize_grocery_items" in data:
                return EnhancedRemindersList.from_data(service, data)
            else:
                return StandardRemindersList.from_data(service, data)
        # return StandardRemindersList.from_data(service, data)
        except Exception as e:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))
            raise e

    def refresh(self) -> None:
        """Refresh the reminders list."""
        self._reminders = self._fetch_reminders()

    def to_dict(self) -> dict[str, Any]:
        return {
            "list_id": self.list_id,
            "name": self.name,
            "deleted": self.deleted,
            "imported": self.imported,
            "is_group": self.is_group,
            "is_linked_to_account": self.is_linked_to_account,
            "reminder_ids": self.reminder_ids,
            "resolution_token_map": self.resolution_token_map,
            "sorting_style": self.sorting_style.value,
            "plugin_fields": self.plugin_fields,
            "record_change_tag": self.record_change_tag,
            "created_date": str(self.created_date),
            "created_by_user": self.created_by_user,
            "created_by_device": self.created_by_device,
            "modified_date": str(self.modified_date),
            "modified_by_user": self.modified_by_user,
            "modified_by_device": self.modified_by_device,
            "deleted_date": str(self.deleted_date) if self.deleted_date else None,
            "deleted_by_user": self.deleted_by_user,
            "deleted_by_device": self.deleted_by_device,
        }

    def to_json(self) -> str:
        """Convert the BaseRemindersList to a JSON string."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    ## Properties

    @property
    def reminders(self) -> dict[str, "BaseReminder"]:
        """Returns the reminders in the list."""
        if self._reminders is None:
            self._reminders = self._fetch_reminders()  # type: ignore
        return self._reminders

    ## Internal Methods

    def _fetch_reminders(self) -> dict[str, BaseReminder]:
        query: dict[str, Any] = self._get_fetch_reminders_payload(
            include_completed=False, lookup_validating_reference=True
        )

        records: list[dict[str, Any]] = []

        ## continuationMarker is allowed to be missing -- loop
        ## continuationMarker is allowed to be present and None -- stop
        ## continuationMarker is allowed to be present and a str -- loop

        while (
            "continuationMarker" not in query or query["continuationMarker"] is not None
        ):
            request: Response = self.service.session.post(
                url=f"{self.service.service_endpoint}/records/query",
                data=json.dumps(query),
                params=self.service.params,
                headers={CONTENT_TYPE: CONTENT_TYPE_TEXT},
            )
            response: dict[str, Any] = request.json()
            records.extend(response["records"])

            query["continuationMarker"] = response.get("continuationMarker", None)

        lists: dict[str, BaseReminder] = {}

        record_objects = []  # [Reminder.from_record(service=self.service, record=record) for record in records]
        for record in record_objects:
            lists[record.name] = record

        return lists

    def _get_fetch_reminders_payload(
        self,
        include_completed: Optional[bool],
        lookup_validating_reference: Optional[bool],
    ) -> dict[str, Any]:
        """Returns the payload for fetching reminders."""
        if include_completed is None:
            include_completed = False

        if lookup_validating_reference is None:
            lookup_validating_reference = True

        return {
            "query": {
                "recordType": "reminderList",
                "filterBy": [
                    {
                        "fieldName": "listID",
                        "comparator": "EQUALS",
                        "fieldValue": {
                            "value": {"recordName": self.list_id, "action": "VALIDATE"},
                            "type": "REFERENCE",
                        },
                    },
                    {
                        "fieldName": "includeCompleted",
                        "comparator": "EQUALS",
                        "fieldValue": {
                            "value": 1 if include_completed else 0,
                            "type": "INT64",
                        },
                    },
                    {
                        "fieldName": "LookupValidatingReference",
                        "comparator": "EQUALS",
                        "fieldValue": {
                            "value": 1 if lookup_validating_reference else 0,
                            "type": "INT64",
                        },
                    },
                ],
            },
            "resultsLimit": 200,
            "zoneID": {
                "zoneName": self.service.zone.zone_id.zone_name,
                "ownerRecordName": self.service.zone.zone_id.owner_record_name,
            },
        }


class SimpleRemindersList(BaseRemindersList):
    """Represents a simple reminders list."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class StandardRemindersList(BaseRemindersList):
    """Represents the user's primary photo libraries."""

    def __init__(
        self,
        expiration_date: Optional[datetime] = None,
        parent_list: Optional["BaseRemindersList"] = None,
        **kwargs,
    ) -> None:
        try:
            super().__init__(**kwargs)
        except Exception as e:
            print(repr(kwargs))
            raise e

        self.expiration_date: Optional[datetime] = expiration_date
        self.parent_list: Optional["BaseRemindersList"] = parent_list

    @staticmethod
    def from_data(
        service: "RemindersService",
        data: dict[str, Any],
    ) -> "StandardRemindersList":
        return StandardRemindersList(
            service=service,
            **data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert the RemindersList to a dict."""

        this_dict = super().to_dict()
        this_dict["expiration_date"] = (
            str(self.expiration_date) if self.expiration_date else None
        )
        this_dict["parent_list"] = (
            self.parent_list.to_dict() if self.parent_list else None
        )

        return this_dict


class SharedRemindersList(StandardRemindersList):
    """Represents the user's primary photo libraries."""

    def __init__(
        self,
        share: dict[str, Any],
        stable_url: str,
        short_guid: str,
        **kwargs,
    ) -> None:
        should_auto_categorize_items = kwargs.pop("should_auto_categorize_items", None)

        super().__init__(**kwargs)
        self.share: dict[str, Any] = share
        self.stable_url: str = stable_url
        self.short_guid: str = short_guid
        # self.expiration_date: Optional[datetime] = expiration_date

        if should_auto_categorize_items:
            self.should_auto_categorize_items: bool = should_auto_categorize_items

    @staticmethod
    def from_data(
        service: "RemindersService",
        data: dict[str, Any],
    ) -> "SharedRemindersList":
        return SharedRemindersList(
            service=service,
            **data,
            # list_id=data["record_id"],
            # list_name=data["fields"].get("Name", "Unknown"),
            # sorting_style=SortingStyleEnum.from_string(data["fields"].get("SortingStyle", "manual")),
            # is_linked_to_account=data["fields"].get("IsLinkedToAccount", 0) == 1,
            # should_categorize_grocery_items=data["fields"].get("ShouldCategorizeGroceryItems", 0) == 1,
            # is_group=data["fields"].get("IsGroup", 0) == 1,
            # resolution_token_map=data["fields"].get("ResolutionTokenMap", {}),
            # imported=data["fields"].get("Imported", 0) == 1,
            # reminder_ids=data["fields"].get("ReminderIDs", []),
            # deleted=data["fields"].get("Deleted", 0) == 1,
            # declared_count=data["fields"].get("Count", 0),
            # plugin_fields=data["fields"].get("PluginFields", {}),
            # record_change_tag=data["fields"].get("RecordChangeTag", ""),
            # expiration_date=data["expiration_date"],
            # share=data["share"],
            # stable_url=data["stable_url"],
            # short_guid=data["short_guid"],
            # created_date=data["created"],
            # created_by_user=data["created_by_user"],
            # created_by_device=data["created_by_device"],
            # modified_date=data["modified"],
            # modified_by_user=data["modified_by_user"],
            # modified_by_device=data["modified_by_device"],
            # deleted_date=data["deleted"],
            # deleted_by_user=data["deleted_by_user"],
            # deleted_by_device=data["deleted_by_device"],
        )

        # @staticmethod
        # def from_record(
        #     service: "RemindersService",
        #     record: dict[str, Any],
        # ) -> "RemindersList":
        #     """Create a RemindersList from a record."""
        #     if record["recordType"] != "List":
        #         raise PyiCloudAPIResponseException(
        #             "Record is not a List", repr({"recordType": record["recordType"]})
        #         )

        #     data = unmarshall(record)
        #     if not data:
        #         raise PyiCloudAPIResponseException("Failed to unmarshall record", repr(record))

        #     if "share" in data:
        #         return SharedRemindersList(
        #             service=service,
        #             list_id=data["record_id"],
        #             list_name=data["fields"].get("Name", ""),
        #             sorting_style=SortingStyleEnum.from_string(data["fields"].get("SortingStyle", "manual")),
        #             is_linked_to_account=data["fields"].get("IsLinkedToAccount", 0) == 1,
        #             should_categorize_grocery_items=data["fields"].get("ShouldCategorizeGroceryItems", 0) == 1,
        #             is_group=data["fields"].get("IsGroup", 0) == 1,
        #             resolution_token_map=data["fields"].get("ResolutionTokenMap", {}),
        #             imported=data["fields"].get("Imported", 0) == 1,
        #             reminder_ids=data["fields"].get("ReminderIDs", []),
        #             deleted= data["fields"].get("Deleted", 0) == 1,
        #             declared_count=data["fields"].get("Count", 0),
        #             plugin_fields=data["fields"].get("PluginFields", {}),
        #             record_change_tag=data["fields"].get("RecordChangeTag", ""),
        #             expiration_date=expiration_date,
        #             share=data["share"],
        #             stableUrl=data["stableURL"],
        #             shortGUID=data["shortGUID"],
        #             created_date=data["created"],
        #             created_by_user=data["created_by_user"],
        #             created_by_device=data["created_by_device"],
        #             modified_date=data["modified"],
        #             modified_by_user=data["modified_by_user"],
        #             modified_by_device=data["modified_by_device"],
        #             deleted_date=data["deleted"],
        #             deleted_by_user=data["deleted_by_user"],
        #             deleted_by_device=data["deleted_by_device"],
        #         )
        #     else:
        #         return RemindersList(
        #             service=service,
        #             list_id=data["record_id"],
        #             list_name=data["fields"].get("Name", ""),
        #             sorting_style=SortingStyleEnum.from_string(data["fields"].get("SortingStyle", "manual")),
        #             is_linked_to_account=data["fields"].get("IsLinkedToAccount", 0) == 1,
        #             should_categorize_grocery_items=data["fields"].get("ShouldCategorizeGroceryItems", 0) == 1,
        #             is_group=data["fields"].get("IsGroup", 0) == 1,
        #             resolution_token_map=data["fields"].get("ResolutionTokenMap", {}),
        #             imported=data["fields"].get("Imported", 0) == 1,
        #             reminder_ids=data["fields"].get("ReminderIDs", []),
        #             deleted= data["fields"].get("Deleted", 0) == 1,
        #             declared_count=data["fields"].get("Count", 0),
        #             plugin_fields=data["fields"].get("PluginFields", {}),
        #             record_change_tag=data["fields"].get("RecordChangeTag", ""),
        #             expiration_date=expiration_date,
        #             created_date=data["created"],
        #             created_by_user=data["created_by_user"],
        #             created_by_device=data["created_by_device"],
        #             modified_date=data["modified"],
        #             modified_by_user=data["modified_by_user"],
        #             modified_by_device=data["modified_by_device"],
        #             deleted_date=data["deleted"],
        #             deleted_by_user=data["deleted_by_user"],
        #             deleted_by_device=data["deleted_by_device"],
        #         )

        # def __repr__(self) -> str:
        #     return self.to_json()
        #     #return f"<RemindersList '{self.name}' ({self.list_id})>"

        # def to_json(self) -> str:
        #     return json.dumps({
        #         "list_id": self.list_id,
        #         "name": self.name,
        #         "sorting_style": self.sorting_style.value,
        #         "is_linked_to_account": self.is_linked_to_account,
        #         "should_categorize_grocery_items": self.should_categorize_grocery_items,
        #         "is_group": self.is_group,
        #         "resolution_token_map": self.resolution_token_map,
        #         "imported": self.imported,
        #         "reminder_ids": self.reminder_ids,
        #         "deleted": self.deleted,
        #         "declared_count": self.declared_count,
        #         "plugin_fields": self.plugin_fields,
        #         "record_change_tag": self.record_change_tag,
        #         "expiration_date": str(self.expiration_date) if self.expiration_date else None,
        #         "created_date": str(self.created_date),
        #         "created_by_user": self.created_by_user,
        #         "created_by_device": self.created_by_device,
        #         "modified_date": str(self.modified_date),
        #         "modified_by_user": self.modified_by_user,
        #         "modified_by_device": self.modified_by_device,
        #         "deleted_date": str(self.deleted_date) if self.deleted_date else None,
        #         "deleted_by_user": self.deleted_by_user,
        #         "deleted_by_device": self.deleted_by_device,
        #     }, indent=2, ensure_ascii=False)

        # def refresh(self) -> None:
        #     """Refresh the reminders list."""
        #     self._reminders: dict[str, BaseReminder] = self._fetch_reminders()

        # def _fetch_reminders(self) -> dict[str, BaseReminder]:
        query: dict[str, Any] = self._get_fetch_reminders_payload(
            include_completed=False, lookup_validating_reference=True
        )

        records: list[dict[str, Any]] = []

        ## continuationMarker is allowed to be missing -- loop
        ## continuationMarker is allowed to be present and None -- stop
        ## continuationMarker is allowed to be present and a str -- loop

        while (
            "continuationMarker" not in query or query["continuationMarker"] is not None
        ):
            request: Response = self.service.session.post(
                url=f"{self.service.service_endpoint}/records/query",
                data=json.dumps(query),
                params=self.service.params,
                headers={CONTENT_TYPE: CONTENT_TYPE_TEXT},
            )
            response: dict[str, Any] = request.json()
            records.extend(response["records"])

            query["continuationMarker"] = response.get("continuationMarker", None)

        lists: dict[str, BaseReminder] = {}

        record_objects = []  # [Reminder.from_record(service=self.service, record=record) for record in records]
        for record in record_objects:
            lists[record.name] = record

        return lists

    def to_dict(self) -> dict[str, Any]:
        this_dict = super().to_dict()
        this_dict["share"] = self.share
        this_dict["stable_url"] = self.stable_url
        this_dict["short_guid"] = self.short_guid
        return this_dict

    # @property
    # def reminders(self) -> dict[str, "BaseReminder"]:
    #     """Returns the reminders in the list."""
    #     if self._reminders is None:
    #         self._reminders = self._fetch_reminders() # type: ignore
    #     return self._reminders


class EnhancedRemindersList(StandardRemindersList):
    """Represents an enhanced reminders list."""

    def __init__(
        self,
        **kwargs,
    ) -> None:
        should_auto_categorize_items = kwargs.pop("should_auto_categorize_items", None)
        should_categorize_grocery_items = kwargs.pop(
            "should_categorize_grocery_items", True
        )
        super().__init__(**kwargs)

        if should_auto_categorize_items:
            self.should_auto_categorize_items: bool = should_auto_categorize_items

        if should_categorize_grocery_items:
            self.should_categorize_grocery_items: bool = should_categorize_grocery_items

    @staticmethod
    def from_data(
        service: "RemindersService",
        data: dict[str, Any],
    ) -> "EnhancedRemindersList":
        return EnhancedRemindersList(
            service=service,
            **data,
        )

    def to_dict(self) -> dict[str, Any]:
        this_dict = super().to_dict()
        this_dict["should_categorize_grocery_items"] = (
            self.should_categorize_grocery_items
        )
        return this_dict


class ListsContainer(Iterable):
    def __init__(self, service: "RemindersService") -> None:
        self.service: RemindersService = service
        self._records: dict[str, BaseRemindersList] = {}

    def __repr__(self) -> str:
        return f"<ListsContainer {len(self.records)}>"

    def __len__(self) -> int:
        """Returns the number of list."""
        return len(self.records)

    def __getitem__(self, id: str) -> BaseRemindersList:
        """Returns a list by id."""
        return self.get_by_id(id)

    def get_by_id(self, list_id: str) -> BaseRemindersList:
        """Returns a list by ID."""
        return self.records[list_id]

    def get_by_name(self, name: str) -> BaseRemindersList:
        """Returns a list by name."""
        for record in self.records.values():
            if record.name == name:
                return record
        raise KeyError(f"List '{name}' not found.")

    def __iter__(self) -> Iterator[BaseRemindersList]:
        """Returns an iterator over the lists."""
        return iter(self.records.values())

    @property
    def records(self) -> dict[str, BaseRemindersList]:
        """Returns the list of RemindersList."""
        if not self._records:
            self._records = self._fetch_lists()
        return self._records

    def lists(self) -> list[BaseRemindersList]:
        """Returns the lists."""
        return list(self.records.values())

    def refresh(self) -> None:
        """Refresh the lists."""
        records = self._fetch_lists()
        print(f"Refreshed lists: {len(records)}")
        _LOGGER.warning(f"Refreshed lists: {len(records)}")
        self._records = records

    def _fetch_lists(self) -> dict[str, BaseRemindersList]:
        query: dict[str, Any] = {
            "query": {
                "recordType": "Lists",
            },
            "resultsLimit": 200,
            "zoneID": self.service.zone.zone_id.to_dict(),
        }

        records: list[dict[str, Any]] = []

        ## continuationMarker is allowed to be missing -- loop
        ## continuationMarker is allowed to be present and None -- stop
        ## continuationMarker is allowed to be present and a str -- loop

        while (
            "continuationMarker" not in query or query["continuationMarker"] is not None
        ):
            request: Response = self.service.session.post(
                url=f"{self.service.service_endpoint}/records/query",
                data=json.dumps(query),
                params=self.service.params,
                headers={CONTENT_TYPE: CONTENT_TYPE_TEXT},
            )
            response: dict[str, Any] = request.json()
            records.extend(response["records"])

            query["continuationMarker"] = response.get("continuationMarker", None)

        lists: dict[str, BaseRemindersList] = {}

        record_objects = [
            StandardRemindersList.from_record(service=self.service, record=record)
            for record in records
        ]
        for record in record_objects:
            lists[record.name] = record

        return lists


class ZoneObject:
    """Represents a reminders zone."""

    @staticmethod
    def from_record(
        service: "RemindersService",
        record: Union[dict[str, Any], None],
    ) -> "ZoneObject":
        """Create a ZoneObject from a record."""

        if not record or record is None or not isinstance(record, dict):
            raise PyiCloudAPIResponseException("Record is empty", repr(record))
        elif "atomic" not in record:
            raise PyiCloudAPIResponseException(
                "Zone record should contain field 'atomic'", repr(record)
            )
        elif "isEligibleForHierarchicalShare" not in record:
            raise PyiCloudAPIResponseException(
                "Zone record should contain field 'isisEligibleForHierarchicalShare'",
                repr(record),
            )
        elif "isEligibleForZoneShare" not in record:
            raise PyiCloudAPIResponseException(
                "Zone record should contain field 'isEligibleForZoneShare'",
                repr(record),
            )
        elif "zoneID" not in record:
            raise PyiCloudAPIResponseException(
                "Zone record should contain field 'zoneID'", repr(record)
            )
        elif not isinstance(record["zoneID"], dict):
            raise PyiCloudAPIResponseException(
                "Zone record's 'zoneID' field should be a dict", repr(record)
            )
        elif "zoneName" not in record["zoneID"]:
            raise PyiCloudAPIResponseException(
                "Zone record's 'zoneID' field should contain 'zoneName'", repr(record)
            )
        elif "zoneType" not in record["zoneID"]:
            raise PyiCloudAPIResponseException(
                "Zone record's 'zoneID' field should contain 'zoneType'", repr(record)
            )
        elif "ownerRecordName" not in record["zoneID"]:
            raise PyiCloudAPIResponseException(
                "Zone record's 'zoneID' field should contain 'ownerRecordName'",
                repr(record),
            )
        elif "syncToken" in record and not isinstance(record["syncToken"], str):
            raise PyiCloudAPIResponseException(
                "Zone records field 'syncToken' is optional but must be a str, if present",
                repr(record),
            )

        return ZoneObject(
            service=service,
            zone_id=record["zoneID"],
            atomic=record["atomic"],
            is_eligible_for_hierarchical_share=record["isEligibleForHierarchicalShare"],
            is_eligible_for_zone_share=record["isEligibleForZoneShare"],
            sync_token=record.get("syncToken", None),
        )

    def __init__(
        self,
        service: "RemindersService",
        zone_id: dict[str, str],
        atomic: bool,
        is_eligible_for_hierarchical_share: bool,
        is_eligible_for_zone_share: bool,
        sync_token: Optional[str],
    ) -> None:
        self.service: RemindersService = service
        self.zone_id: ZoneIDObject = ZoneIDObject.from_record(zone_id)
        self.url: str = f"{self.service.service_endpoint}/records/query?{urlencode(self.service.params)}"
        self.atomic: bool = atomic
        self.is_eligible_for_hierarchical_share: bool = (
            is_eligible_for_hierarchical_share
        )
        self.is_eligible_for_zone_share: bool = is_eligible_for_zone_share
        self.sync_token: Optional[str] = sync_token


class ZoneIDObject:
    """Represents a zone ID."""

    @staticmethod
    def from_record(record: dict[str, str]) -> "ZoneIDObject":
        """Create a ZoneIDObject from a record."""
        if not isinstance(record, dict):
            raise PyiCloudAPIResponseException("Record is not a dict", repr(record))
        elif "zoneName" not in record:
            raise PyiCloudAPIResponseException(
                "Record should contain 'zoneName'", repr(record)
            )
        elif "zoneType" not in record:
            raise PyiCloudAPIResponseException(
                "Record should contain 'zoneType'", repr(record)
            )
        elif "ownerRecordName" not in record:
            raise PyiCloudAPIResponseException(
                "Record should contain 'ownerRecordName'", repr(record)
            )

        return ZoneIDObject(
            zone_name=record["zoneName"],
            zone_type=record["zoneType"],
            owner_record_name=record["ownerRecordName"],
        )

    def __init__(self, zone_name: str, zone_type: str, owner_record_name: str) -> None:
        self.zone_name: str = zone_name
        self.zone_type: str = zone_type
        self.owner_record_name: str = owner_record_name

    def to_dict(self) -> dict[str, str]:
        """Convert the ZoneIDObject to a dict."""
        return {
            "zoneName": self.zone_name,
            "zoneType": self.zone_type,
            "ownerRecordName": self.owner_record_name,
        }

    def __repr__(self) -> str:
        return json.dumps(
            {
                "zoneName": self.zone_name,
                "zoneType": self.zone_type,
                "ownerRecordName": self.owner_record_name,
            },
            indent=2,
            ensure_ascii=False,
        )


class ZonesContainer(Iterable):
    def __init__(self, service: "RemindersService") -> None:
        self.service: RemindersService = service
        self._records: list[ZoneObject] = []

    def __len__(self) -> int:
        """Returns the number of zones."""
        return len(self.records)

    def __getitem__(self, name: str) -> ZoneObject:
        """Returns a zone by name."""
        for record in self.records:
            if record.zone_id.zone_name == name:
                return record
        raise KeyError(f"Zone '{name}' not found.")

    def __iter__(self) -> Iterator[ZoneObject]:
        """Returns an iterator over the zones."""
        return iter(self.records)

    @property
    def records(self) -> list[ZoneObject]:
        """Returns the list of zones."""
        if not self._records:
            self._records = self._fetch_zones()
        return self._records

    def refresh(self) -> None:
        """Refresh the zones."""
        self._records = self._fetch_zones()

    def _fetch_zones(self) -> list[ZoneObject]:
        request: Response = self.service.session.get(
            url=f"{self.service.service_endpoint}/zones/list",
            params=self.service.params,
            headers={CONTENT_TYPE: CONTENT_TYPE_TEXT},
        )
        response: dict[str, Any] = request.json()

        return [
            ZoneObject.from_record(service=self.service, record=record)
            for record in response.get("zones", [])
        ]


class RemindersService(BaseService):
    """The 'Reminders' iCloud service."""

    def __init__(
        self, service_root: str, session: PyiCloudSession, params: dict[str, Any]
    ) -> None:
        BaseService.__init__(
            self,
            service_root=service_root,
            session=session,
            params=params,
        )
        self.service_endpoint: str = (
            f"{self.service_root}/database/1/com.apple.reminders/production/private"
        )

        self.params.update({"remapEnums": True, "getCurrentSyncToken": True})

        self.all_zones: ZonesContainer = ZonesContainer(self)

        zone = self.all_zones[PRIMARY_ZONE["zoneName"]]
        if not zone:
            raise PyiCloudServiceNotActivatedException(
                f"Could not find Primary Zone '{PRIMARY_ZONE['zoneName']}'."
            )

        self.zone: ZoneObject = zone

        self._lists: ListsContainer = ListsContainer(self)

        self.refresh()

    @property
    def lists(self) -> list[BaseRemindersList]:
        """Returns the lists."""
        return self._lists.lists()

    @property
    def lists_container(self) -> ListsContainer:
        """Returns the ListsContainer."""
        return self._lists

    # def post(self, title, description="", collection=None, due_date=None):
    #     """Adds a new reminder."""
    #     pguid = "tasks"
    #     if collection and collection in self.collections:
    #         pguid = self.collections[collection]["guid"]

    #     params_reminders = dict(self.params)
    #     params_reminders.update(
    #         {"clientVersion": "4.0", "lang": "en-us", "usertz": get_localzone_name()}
    #     )

    #     due_dates = None
    #     if due_date:
    #         due_dates = [
    #             int(f"{due_date.year}{due_date.month:02}{due_date.day:02}"),
    #             due_date.year,
    #             due_date.month,
    #             due_date.day,
    #             due_date.hour,
    #             due_date.minute,
    #         ]

    #     req = self.session.post(
    #         f"{self.service_root}/rd/reminders/tasks",
    #         data=json.dumps(
    #             {
    #                 "Reminders": {
    #                     "title": title,
    #                     "description": description,
    #                     "pGuid": pguid,
    #                     "etag": None,
    #                     "order": None,
    #                     "priority": 0,
    #                     "recurrence": None,
    #                     "alarms": [],
    #                     "startDate": None,
    #                     "startDateTz": None,
    #                     "startDateIsAllDay": False,
    #                     "completedDate": None,
    #                     "dueDate": due_dates,
    #                     "dueDateIsAllDay": False,
    #                     "lastModifiedDate": None,
    #                     "createdDate": None,
    #                     "isFamily": None,
    #                     "createdDateExtended": int(time.time() * 1000),
    #                     "guid": str(uuid.uuid4()),
    #                 },
    #                 "ClientState": {"Collections": list(self.collections.values())},
    #             }
    #         ),
    #         params=params_reminders,
    #     )
    #     return req.ok

    def refresh(self) -> None:
        """Refresh the list of ReminderList."""
        self._lists.refresh()
