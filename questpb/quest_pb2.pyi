from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Outcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTCOME_UNSPECIFIED: _ClassVar[Outcome]
    OUTCOME_COMPLETED: _ClassVar[Outcome]
    OUTCOME_SKIPPED: _ClassVar[Outcome]
    OUTCOME_ABANDONED: _ClassVar[Outcome]
OUTCOME_UNSPECIFIED: Outcome
OUTCOME_COMPLETED: Outcome
OUTCOME_SKIPPED: Outcome
OUTCOME_ABANDONED: Outcome

class ResolveQuestRequest(_message.Message):
    __slots__ = ("instance_id", "outcome")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    outcome: Outcome
    def __init__(self, instance_id: _Optional[str] = ..., outcome: _Optional[_Union[Outcome, str]] = ...) -> None: ...

class Quest(_message.Message):
    __slots__ = ("instance_id", "quest_id", "category", "difficulty", "text", "status", "created_at")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    QUEST_ID_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DIFFICULTY_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    quest_id: int
    category: str
    difficulty: str
    text: str
    status: str
    created_at: float
    def __init__(self, instance_id: _Optional[str] = ..., quest_id: _Optional[int] = ..., category: _Optional[str] = ..., difficulty: _Optional[str] = ..., text: _Optional[str] = ..., status: _Optional[str] = ..., created_at: _Optional[float] = ...) -> None: ...

class ResolveQuestResponse(_message.Message):
    __slots__ = ("quest", "changed")
    QUEST_FIELD_NUMBER: _ClassVar[int]
    CHANGED_FIELD_NUMBER: _ClassVar[int]
    quest: Quest
    changed: bool
    def __init__(self, quest: _Optional[_Union[Quest, _Mapping]] = ..., changed: _Optional[bool] = ...) -> None: ...
