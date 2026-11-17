"""主动群聊共享 Redis 状态。

Redis 是这个功能的协调面而不是可选缓存；任一读写异常都向上抛出，调用方
必须因此关闭本轮主动发送，不能在各容器内自行降级。所有竞争敏感更新集中在
Lua 脚本中，避免 Python 侧的读改写窗口。
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
HEARTBEAT_TTL_SECONDS = 180
GROUP_MEMBER_TTL_SECONDS = 1800
PLAN_TTL_SECONDS = 48 * 60 * 60
COOLDOWN_SECONDS = 90 * 60
LEASE_TTL_SECONDS = 120
GRACE_SECONDS = 10 * 60
_PREFIX = "dingtalk-ai-bot:proactive:v1"


class ProactiveStateError(RuntimeError):
    """Redis 中的主动调度状态缺失、损坏或违反接口契约。"""


_CREATE_PLAN_LUA = """
local current_epoch = redis.call('GET', KEYS[3])
if not current_epoch or current_epoch ~= ARGV[2] then
    return {0, 'epoch'}
end
local existing = redis.call('GET', KEYS[1])
local created = redis.call('HGET', KEYS[2], 'created')
if existing then
    if not created or created ~= ARGV[2] then
        return {0, 'state'}
    end
    local existing_ok, existing_plan = pcall(cjson.decode, existing)
    if not existing_ok or type(existing_plan) ~= 'table' or type(existing_plan.slots) ~= 'table' then
        return {0, 'state'}
    end
    for i = 0, #existing_plan.slots - 1 do
        local marker = redis.call('HGET', KEYS[2], 'slot:' .. i)
        if not marker or (marker ~= '0' and marker ~= '1') then
            return {0, 'state'}
        end
    end
    return {2, existing}
end
if created then
    return {0, 'state'}
end
local stored = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3], 'NX')
if not stored then
    existing = redis.call('GET', KEYS[1])
    created = redis.call('HGET', KEYS[2], 'created')
    if existing and created == ARGV[2] then
        local raced_ok, raced_plan = pcall(cjson.decode, existing)
        if not raced_ok or type(raced_plan) ~= 'table' or type(raced_plan.slots) ~= 'table' then
            return {0, 'state'}
        end
        for i = 0, #raced_plan.slots - 1 do
            local marker = redis.call('HGET', KEYS[2], 'slot:' .. i)
            if not marker or (marker ~= '0' and marker ~= '1') then
                return {0, 'state'}
            end
        end
        return {2, existing}
    end
    return {0, 'state'}
end
local decoded_ok, plan = pcall(cjson.decode, ARGV[1])
if not decoded_ok or type(plan) ~= 'table' or type(plan.slots) ~= 'table' then
    return {0, 'state'}
end
redis.call('HSET', KEYS[2], 'created', ARGV[2])
for i = 0, #plan.slots - 1 do
    redis.call('HSET', KEYS[2], 'slot:' .. i, '0')
end
redis.call('EXPIRE', KEYS[2], ARGV[3])
return {1, ARGV[1]}
"""


_CLAIM_LUA = """
local current_epoch = redis.call('GET', KEYS[8])
if not current_epoch or current_epoch ~= ARGV[17] then
    return {0, 'epoch'}
end
local created_epoch = redis.call('HGET', KEYS[2], 'created')
if not created_epoch or created_epoch ~= ARGV[17] then
    return {0, 'state'}
end
local raw = redis.call('GET', KEYS[1])
if not raw then
    return {0, 'missing-plan'}
end

local decoded_ok, plan = pcall(cjson.decode, raw)
if not decoded_ok or type(plan) ~= 'table' then
    return {0, 'invalid-state'}
end
if type(plan.day) ~= 'string' or plan.day ~= ARGV[1] then
    return {0, 'invalid-state'}
end
if type(plan.seed) ~= 'string' or string.len(plan.seed) == 0 then
    return {0, 'invalid-state'}
end
if type(plan.slots) ~= 'table' then
    return {0, 'invalid-state'}
end
local slot_count = #plan.slots
if slot_count < 2 or slot_count > 4 then
    return {0, 'invalid-state'}
end
local previous_slot = nil
for i = 1, slot_count do
    local current_slot = tonumber(plan.slots[i])
    if not current_slot or current_slot ~= current_slot then
        return {0, 'invalid-state'}
    end
    if previous_slot and current_slot - previous_slot < tonumber(ARGV[7]) then
        return {0, 'invalid-state'}
    end
    previous_slot = current_slot
end

local slot_index = tonumber(ARGV[2])
if not slot_index or slot_index % 1 ~= 0 or slot_index < 0 or slot_index >= slot_count then
    return {0, 'bounds'}
end
local marker_name = 'slot:' .. ARGV[2]
local marker = redis.call('HGET', KEYS[2], marker_name)
if not marker or (marker ~= '0' and marker ~= '1') then
    return {0, 'invalid-state'}
end
local slot = tonumber(plan.slots[slot_index + 1])
local now = tonumber(ARGV[4])
local grace = tonumber(ARGV[8])
if not slot or not now or not grace then
    return {0, 'invalid-state'}
end
if now < slot or now > slot + grace then
    return {0, 'window'}
end

local consumed_slots = 0
local last_attempt_raw = redis.call('HGET', KEYS[2], 'last_attempt')
local last_speaker = redis.call('GET', KEYS[6]) or ''
for i = 0, slot_count - 1 do
    local all_marker = redis.call('HGET', KEYS[2], 'slot:' .. i)
    if not all_marker or (all_marker ~= '0' and all_marker ~= '1') then
        return {0, 'invalid-state'}
    end
    if all_marker == '1' then
        consumed_slots = consumed_slots + 1
    end
end
if consumed_slots > 0 then
    local previous_attempt = tonumber(last_attempt_raw)
    if not previous_attempt or previous_attempt < 0 or previous_attempt > now or last_speaker == '' then
        return {0, 'invalid-state'}
    end
end

if redis.call('EXISTS', KEYS[4]) == 0 or redis.call('EXISTS', KEYS[5]) == 0 then
    return {0, 'offline'}
end
local heartbeat_at = tonumber(redis.call('GET', KEYS[4]))
local member_at = tonumber(redis.call('GET', KEYS[5]))
if not heartbeat_at or not member_at then
    return {0, 'invalid-state'}
end
if heartbeat_at > now or now - heartbeat_at > tonumber(ARGV[10]) then
    return {0, 'offline'}
end
if member_at > now or now - member_at > tonumber(ARGV[11]) then
    return {0, 'offline'}
end

local prefix = ARGV[12]
local corp_hash = ARGV[13]
local cid_hash = ARGV[14]
local hash_ok, hash_map = pcall(cjson.decode, ARGV[15])
local rank_ok, rank_map = pcall(cjson.decode, ARGV[16])
if not hash_ok or type(hash_map) ~= 'table' or not rank_ok or type(rank_map) ~= 'table' then
    return {0, 'invalid-state'}
end
local function digest(value)
    if redis.sha1hex then
        return redis.sha1hex(value)
    end
    return hash_map[value]
end
local function rank_digest(value)
    if redis.sha1hex then
        return redis.sha1hex(plan.seed .. ':' .. ARGV[2] .. ':' .. value)
    end
    return rank_map[value]
end
local bot_hash_prefix = prefix .. ':heartbeat-index:' .. corp_hash .. ':'
local heartbeat_prefix = prefix .. ':heartbeat:' .. corp_hash .. ':'
local member_prefix = prefix .. ':group-member:' .. corp_hash .. ':' .. cid_hash .. ':'
local candidates = redis.call('SMEMBERS', KEYS[7])
local candidate_count = 0
local best_bot = nil
local best_digest = nil
local best_other_bot = nil
local best_other_digest = nil
for _, candidate in ipairs(candidates) do
    local candidate_hash = digest(candidate)
    if not candidate_hash then
        return {0, 'invalid-state'}
    end
    local candidate_member_raw = redis.call('GET', member_prefix .. candidate_hash)
    if candidate_member_raw then
        local candidate_member_at = tonumber(candidate_member_raw)
        if not candidate_member_at then
            return {0, 'invalid-state'}
        end
        if candidate_member_at <= now and now - candidate_member_at <= tonumber(ARGV[11]) then
            local instance_set = redis.call('SMEMBERS', bot_hash_prefix .. candidate_hash)
            local active_instances = 0
            for _, instance in ipairs(instance_set) do
                local instance_hash = digest(instance)
                if not instance_hash then
                    return {0, 'invalid-state'}
                end
                local heartbeat_raw = redis.call('GET', heartbeat_prefix .. candidate_hash .. ':' .. instance_hash)
                if heartbeat_raw then
                    local candidate_heartbeat_at = tonumber(heartbeat_raw)
                    if not candidate_heartbeat_at then
                        return {0, 'invalid-state'}
                    end
                    if candidate_heartbeat_at <= now and now - candidate_heartbeat_at <= tonumber(ARGV[10]) then
                        active_instances = active_instances + 1
                    end
                end
            end
            if active_instances == 1 then
                candidate_count = candidate_count + 1
                local candidate_digest = rank_digest(candidate)
                if not candidate_digest then
                    return {0, 'invalid-state'}
                end
                if not best_digest or candidate_digest < best_digest then
                    best_bot = candidate
                    best_digest = candidate_digest
                end
                if candidate ~= last_speaker and (not best_other_digest or candidate_digest < best_other_digest) then
                    best_other_bot = candidate
                    best_other_digest = candidate_digest
                end
            end
        end
    end
end
local winner = best_bot
if last_speaker ~= '' and candidate_count > 1 then
    winner = best_other_bot
end
if not winner or winner ~= ARGV[3] then
    return {0, 'winner'}
end
if redis.call('GET', KEYS[3]) then
    return {0, 'leased'}
end

if marker == '1' then
    return {0, 'consumed'}
end

local last_attempt = last_attempt_raw
if last_attempt then
    last_attempt = tonumber(last_attempt)
    if not last_attempt then
        return {0, 'invalid-state'}
    end
    if now - last_attempt < tonumber(ARGV[7]) then
        return {0, 'cooldown'}
    end
end

local lease_ok = redis.call('SET', KEYS[3], ARGV[5], 'EX', ARGV[6], 'NX')
if not lease_ok then
    return {0, 'leased'}
end
redis.call('HSET', KEYS[2], marker_name, '1', 'last_attempt', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[9])
redis.call('SET', KEYS[6], ARGV[3], 'EX', ARGV[9])
return {1, ARGV[5]}
"""

_OWNS_LUA = """
local current_epoch = redis.call('GET', KEYS[2])
if not current_epoch or current_epoch ~= ARGV[2] then
    return -1
end
local current = redis.call('GET', KEYS[1])
if current and current == ARGV[1] then
    return 1
end
return 0
"""

_FINISH_LUA = """
local current_epoch = redis.call('GET', KEYS[2])
if not current_epoch or current_epoch ~= ARGV[2] then
    return -1
end
local current = redis.call('GET', KEYS[1])
if current and current == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _digest(value: Any) -> str:
    text = _text(value)
    if not text:
        raise ValueError("主动状态键组件不能为空")
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        local = value.replace(tzinfo=BEIJING) if value.tzinfo is None else value
        value = local.timestamp()
    elif isinstance(value, date):
        value = datetime.combine(value, datetime.min.time(), tzinfo=BEIJING).timestamp()
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("主动状态时间必须是有限数")
    return result


def _day_text(day: date | datetime | str) -> str:
    if isinstance(day, datetime):
        day = day.astimezone(BEIJING).date() if day.tzinfo else day.date()
    if isinstance(day, date):
        return day.isoformat()
    text = _text(day).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("主动状态日期必须是 YYYY-MM-DD") from exc
    return parsed.isoformat()


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return value


class ProactiveStore:
    """同步 Redis 状态适配器，供调度器通过 ``asyncio.to_thread`` 调用。"""

    def __init__(
        self,
        redis_client: Any,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        bot_id: str | None = None,
        robot_code: str | None = None,
        instance_id: str | None = None,
    ):
        self.redis = redis_client
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.bot_id = bot_id.strip() if isinstance(bot_id, str) and bot_id.strip() else ""
        self.robot_code = robot_code.strip() if isinstance(robot_code, str) and robot_code.strip() else ""
        self.instance_id = (
            instance_id.strip()
            if isinstance(instance_id, str) and instance_id.strip()
            else uuid.uuid4().hex
        )
        self._volatile_epoch: str | None = None
        self._volatile_plan_mirrors: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _key(self, kind: str, corp: str, cid: str, bot: str | None = None, day: str | None = None) -> str:
        parts = [_PREFIX, kind, _digest(corp), _digest(cid)]
        if bot is not None:
            parts.append(_digest(bot))
        if day is not None:
            parts.append(_digest(day))
        return ":".join(parts)

    def _heartbeat_key(self, corp: str, bot: str, instance_id: str | None = None) -> str:
        instance = self.instance_id if instance_id is None else instance_id
        return f"{_PREFIX}:heartbeat:{_digest(corp)}:{_digest(bot)}:{_digest(instance)}"

    def _heartbeat_index_key(self, corp: str, bot: str) -> str:
        return f"{_PREFIX}:heartbeat-index:{_digest(corp)}:{_digest(bot)}"

    def _group_index_key(self, corp: str, cid: str) -> str:
        return f"{_PREFIX}:group-index:{_digest(corp)}:{_digest(cid)}"

    def _group_member_key(self, corp: str, cid: str, bot: str) -> str:
        return f"{_PREFIX}:group-member:{_digest(corp)}:{_digest(cid)}:{_digest(bot)}"

    def _bot_pin_key(self, bot: str) -> str:
        return f"{_PREFIX}:bot-pin:{_digest(bot)}"

    def _group_identity_key(self, corp: str, cid: str, bot: str) -> str:
        return f"{_PREFIX}:bot-identity:{_digest(corp)}:{_digest(cid)}:{_digest(bot)}"

    def _epoch_key(self, corp: str) -> str:
        return f"{_PREFIX}:epoch:{_digest(corp)}"

    def _epoch_path(self, corp: str) -> Path:
        if self.state_dir is None:
            raise ProactiveStateError("主动状态未配置 epoch 目录")
        return self.state_dir / f"{_digest(corp)}.epoch"

    def _plan_mirror_path(self, corp: str, cid: str, day: str) -> Path:
        if self.state_dir is None:
            raise ProactiveStateError("主动状态未配置计划镜像目录")
        return self.state_dir / _digest(corp) / _digest(cid) / f"{_digest(day)}.plan"

    @staticmethod
    def _write_exclusive(path: Path, payload: bytes) -> bool:
        try:
            descriptor = os.open(
                str(path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return False
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name != "nt":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True

    def _read_plan_mirror(self, corp: str, cid: str, day: str) -> dict[str, Any] | None:
        if self.state_dir is None:
            value = self._volatile_plan_mirrors.get((str(corp), str(cid), day))
            if value is None:
                return None
            return {"seed": value["seed"], "slots": list(value["slots"]), "day": value["day"]}
        path = self._plan_mirror_path(corp, cid, day)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return self._parse_plan(handle.read(), day)
        except FileNotFoundError:
            return None
        except ProactiveStateError:
            raise
        except OSError as exc:
            raise ProactiveStateError("主动计划镜像不可读") from exc

    def _create_plan_mirror(self, corp: str, cid: str, day: str, plan: dict[str, Any]) -> bool:
        stored = {"seed": plan["seed"], "slots": list(plan["slots"]), "day": day}
        if self.state_dir is None:
            key = (str(corp), str(cid), day)
            if key in self._volatile_plan_mirrors:
                return False
            self._volatile_plan_mirrors[key] = stored
            return True
        path = self._plan_mirror_path(corp, cid, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._write_exclusive(path, payload)

    def _plan_key(self, corp: str, cid: str, day: date | datetime | str) -> str:
        return self._key("plan", corp, cid, day=_day_text(day))

    def _daily_state_key(self, corp: str, cid: str, day: date | datetime | str) -> str:
        return self._key("daily-state", corp, cid, day=_day_text(day))

    def _lease_key(self, corp: str, cid: str) -> str:
        return self._key("lease", corp, cid)

    def _last_speaker_key(self, corp: str, cid: str) -> str:
        return self._key("last-speaker", corp, cid)

    @staticmethod
    def _validate_epoch(value: Any) -> str:
        value = _decode(value)
        if not isinstance(value, str) or not value.strip():
            raise ProactiveStateError("主动 epoch 状态损坏")
        text = value.strip()
        try:
            uuid.UUID(text)
        except (ValueError, AttributeError) as exc:
            raise ProactiveStateError("主动 epoch 状态损坏") from exc
        return text

    def _read_epoch_file(self, path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return self._validate_epoch(handle.read())
        except FileNotFoundError:
            return None
        except ProactiveStateError:
            raise
        except OSError as exc:
            raise ProactiveStateError("主动 epoch 文件不可读") from exc

    @staticmethod
    def _create_epoch_file(path: Path) -> str | None:
        value = uuid.uuid4().hex
        if not ProactiveStore._write_exclusive(path, value.encode("ascii")):
            return None
        return value

    def ensure_epoch(self, corp: str) -> str:
        """验证磁盘与 Redis 的同一 epoch，防止 flushdb 后重建当天计划。"""

        epoch_key = self._epoch_key(corp)
        remote = _decode(self.redis.get(epoch_key))
        if self.state_dir is None:
            if remote is None:
                if self._volatile_epoch is not None:
                    raise ProactiveStateError("Redis epoch 丢失，拒绝重建主动状态")
                candidate = uuid.uuid4().hex
                if not self.redis.set(epoch_key, candidate, nx=True):
                    remote = _decode(self.redis.get(epoch_key))
                    if remote is None:
                        raise ProactiveStateError("主动 epoch 创建竞争后状态丢失")
                else:
                    remote = candidate
            checked = self._validate_epoch(remote)
            if self._volatile_epoch is not None and checked != self._volatile_epoch:
                raise ProactiveStateError("主动 epoch 与进程状态不一致")
            self._volatile_epoch = checked
            return checked

        path = self._epoch_path(corp)
        local = self._read_epoch_file(path)
        if local is None:
            if remote is not None:
                raise ProactiveStateError("epoch 文件缺失，拒绝重建主动状态")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            local = self._create_epoch_file(path)
            if local is None:
                raise ProactiveStateError("epoch 正在创建，保守暂停主动状态")
            created = self.redis.set(epoch_key, local, nx=True)
            if not created:
                remote = _decode(self.redis.get(epoch_key))
                if remote != local:
                    raise ProactiveStateError("Redis epoch 与新建文件不一致")
            return local
        if remote is None:
            raise ProactiveStateError("Redis epoch 缺失，拒绝重建主动状态")
        if self._validate_epoch(remote) != local:
            raise ProactiveStateError("磁盘与 Redis epoch 不一致")
        return local

    def _validate_bot(self, bot: str) -> str:
        value = _text(bot).strip()
        if not value:
            raise ProactiveStateError("主动机器人 ID 不能为空")
        if self.bot_id and value != self.bot_id:
            raise ProactiveStateError("主动机器人 ID 与进程元数据不一致")
        return value

    def _ensure_robot_pin(self, bot: str) -> None:
        key = self._bot_pin_key(bot)
        current = _decode(self.redis.get(key))
        if current is None:
            if not self.robot_code:
                return
            if self.redis.set(key, self.robot_code, nx=True):
                return
            current = _decode(self.redis.get(key))
        if not isinstance(current, str) or not current:
            raise ProactiveStateError("机器人 robotCode 绑定状态损坏")
        if not self.robot_code or current != self.robot_code:
            raise ProactiveStateError("机器人 robotCode 混配")

    def _ensure_group_identity(self, corp: str, cid: str, bot: str, bot_sender_id: str | None) -> None:
        if bot_sender_id is None:
            return
        sender = _text(bot_sender_id).strip()
        if not sender:
            return
        key = self._group_identity_key(corp, cid, bot)
        current = _decode(self.redis.get(key))
        if current is None:
            if self.redis.set(key, sender, nx=True):
                return
            current = _decode(self.redis.get(key))
        if current != sender:
            raise ProactiveStateError("群内机器人 sender 身份混配")

    def matches_group_identity(self, corp: str, cid: str, bot: str, sender: str) -> bool:
        """只读核对已登记的群内机器人 sender 身份，不为缺失状态补写。"""

        self.ensure_epoch(corp)
        bot = self._validate_bot(bot)
        expected = _text(sender).strip()
        if not expected:
            return False
        current = _decode(self.redis.get(self._group_identity_key(corp, cid, bot)))
        return isinstance(current, str) and bool(current) and current == expected

    def heartbeat(self, corp: str, bot: str, now: Any) -> bool:
        self.ensure_epoch(corp)
        bot = self._validate_bot(bot)
        self._ensure_robot_pin(bot)
        key = self._heartbeat_key(corp, bot)
        self.redis.set(
            key,
            str(_epoch(now)),
            ex=HEARTBEAT_TTL_SECONDS,
        )
        index_key = self._heartbeat_index_key(corp, bot)
        self.redis.sadd(index_key, self.instance_id)
        self.redis.expire(index_key, PLAN_TTL_SECONDS)
        return True

    def register_group(
        self,
        corp: str,
        cid: str,
        bot: str,
        now: Any,
        bot_sender_id: str | None = None,
    ) -> bool:
        self.ensure_epoch(corp)
        bot = self._validate_bot(bot)
        self._ensure_robot_pin(bot)
        self._ensure_group_identity(corp, cid, bot, bot_sender_id)
        timestamp = str(_epoch(now))
        self.redis.set(
            self._group_member_key(corp, cid, bot),
            timestamp,
            ex=GROUP_MEMBER_TTL_SECONDS,
        )
        index_key = self._group_index_key(corp, cid)
        self.redis.sadd(index_key, bot)
        self.redis.expire(index_key, PLAN_TTL_SECONDS)
        return True

    def eligible_bots(self, corp: str, cid: str, now: Any) -> list[str]:
        self.ensure_epoch(corp)
        current = _epoch(now)
        members = self.redis.smembers(self._group_index_key(corp, cid))
        candidates: list[str] = []
        for member in members:
            bot = _decode(member)
            if not isinstance(bot, str) or not bot:
                raise ProactiveStateError("群机器人登记状态损坏")
            instances = self.redis.smembers(self._heartbeat_index_key(corp, bot))
            active_instances: list[str] = []
            for instance_value in instances:
                instance = _decode(instance_value)
                if not isinstance(instance, str) or not instance:
                    raise ProactiveStateError("机器人实例登记状态损坏")
                heartbeat_raw = _decode(self.redis.get(self._heartbeat_key(corp, bot, instance)))
                if heartbeat_raw is None:
                    continue
                try:
                    heartbeat_at = float(heartbeat_raw)
                except (TypeError, ValueError) as exc:
                    raise ProactiveStateError("机器人在线状态损坏") from exc
                if not math.isfinite(heartbeat_at):
                    raise ProactiveStateError("机器人在线状态损坏")
                if heartbeat_at <= current <= heartbeat_at + HEARTBEAT_TTL_SECONDS:
                    active_instances.append(instance)
            member_raw = _decode(self.redis.get(self._group_member_key(corp, cid, bot)))
            if not active_instances or member_raw is None:
                continue
            try:
                member_at = float(member_raw)
            except (TypeError, ValueError) as exc:
                raise ProactiveStateError("群成员状态损坏") from exc
            if not math.isfinite(member_at):
                raise ProactiveStateError("群成员状态损坏")
            if (
                len(active_instances) == 1
                and member_at <= current <= member_at + GROUP_MEMBER_TTL_SECONDS
            ):
                candidates.append(bot)
        return sorted(set(candidates))

    @staticmethod
    def _validate_slots(slots: Iterable[Any], expected_day: str | None = None) -> list[float]:
        values = list(slots)
        if not 2 <= len(values) <= 4:
            raise ProactiveStateError("每日主动时隙数量损坏")
        result: list[float] = []
        for value in values:
            try:
                converted = float(value)
            except (TypeError, ValueError) as exc:
                raise ProactiveStateError("主动时隙不是数字") from exc
            if not math.isfinite(converted):
                raise ProactiveStateError("主动时隙不是有限数字")
            result.append(converted)
        if any(later <= earlier for earlier, later in zip(result, result[1:])):
            raise ProactiveStateError("主动时隙顺序损坏")
        if any(later - earlier < COOLDOWN_SECONDS for earlier, later in zip(result, result[1:])):
            raise ProactiveStateError("主动时隙间隔不足")
        if expected_day is not None:
            for value in result:
                local = datetime.fromtimestamp(value, BEIJING)
                if local.date().isoformat() != expected_day:
                    raise ProactiveStateError("主动时隙日期损坏")
                local_time = local.time()
                if not (
                    time(9, 0) <= local_time < time(12, 0)
                    or time(14, 0) <= local_time < time(18, 0)
                ):
                    raise ProactiveStateError("主动时隙工作窗口损坏")
        return result

    @classmethod
    def _parse_plan(cls, raw: Any, expected_day: str) -> dict[str, Any]:
        raw = _decode(raw)
        if not isinstance(raw, str):
            raise ProactiveStateError("主动计划状态不是文本")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProactiveStateError("主动计划状态不是有效 JSON") from exc
        if not isinstance(value, dict) or value.get("day") != expected_day:
            raise ProactiveStateError("主动计划日期状态损坏")
        seed = value.get("seed")
        if not isinstance(seed, str) or not seed:
            raise ProactiveStateError("主动计划种子状态损坏")
        slots = cls._validate_slots(value.get("slots", []), expected_day)
        return {"seed": seed, "slots": slots, "day": expected_day}

    def get_or_create_plan(
        self,
        corp: str,
        cid: str,
        day: date | datetime | str,
        slots: Iterable[Any],
    ) -> dict[str, Any]:
        epoch = self.ensure_epoch(corp)
        day_value = _day_text(day)
        candidate_slots = self._validate_slots(slots, day_value)
        plan_key = self._plan_key(corp, cid, day_value)
        mirror = self._read_plan_mirror(corp, cid, day_value)
        raw = self.redis.get(plan_key)
        if mirror is None:
            if raw is not None:
                raise ProactiveStateError("Redis 计划存在但不可变镜像缺失")
            mirror = {
                "seed": secrets.token_hex(16),
                "slots": candidate_slots,
                "day": day_value,
            }
            if not self._create_plan_mirror(corp, cid, day_value, mirror):
                raise ProactiveStateError("主动计划镜像创建竞争，保守暂停")
        elif raw is None:
            raise ProactiveStateError("不可变计划镜像存在但 Redis 计划缺失")
        else:
            stored = self._parse_plan(raw, day_value)
            if stored != mirror:
                raise ProactiveStateError("Redis 计划与不可变镜像不一致")

        payload = json.dumps(mirror, ensure_ascii=False, separators=(",", ":"))
        result = self.redis.eval(
            _CREATE_PLAN_LUA,
            3,
            plan_key,
            self._daily_state_key(corp, cid, day_value),
            self._epoch_key(corp),
            payload,
            epoch,
            str(PLAN_TTL_SECONDS),
        )
        code, detail = self._claim_result(result)
        if code == 0:
            raise ProactiveStateError(f"主动计划状态不可用: {detail}")
        plan = self._parse_plan(detail, day_value)
        if plan != mirror:
            raise ProactiveStateError("Redis 计划与不可变镜像不一致")
        return plan

    def last_speaker(self, corp: str, cid: str) -> str:
        self.ensure_epoch(corp)
        value = _decode(self.redis.get(self._last_speaker_key(corp, cid)))
        if value is None:
            return ""
        if not isinstance(value, str) or not value.strip():
            raise ProactiveStateError("上次发言机器人状态损坏")
        return value

    @staticmethod
    def _claim_result(result: Any) -> tuple[int, str]:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ProactiveStateError("主动抢占返回状态损坏")
        try:
            code = int(_decode(result[0]))
        except (TypeError, ValueError) as exc:
            raise ProactiveStateError("主动抢占返回码损坏") from exc
        reason = _decode(result[1])
        if not isinstance(reason, str):
            raise ProactiveStateError("主动抢占原因损坏")
        return code, reason

    def _claim_hash_maps(
        self,
        corp: str,
        cid: str,
        day: str,
        slot_index: int,
    ) -> tuple[str, str]:
        raw_plan = self.redis.get(self._plan_key(corp, cid, day))
        if raw_plan is None:
            raise ProactiveStateError("主动计划状态缺失")
        plan = self._parse_plan(raw_plan, day)
        hash_map: dict[str, str] = {}
        rank_map: dict[str, str] = {}
        for raw_bot in self.redis.smembers(self._group_index_key(corp, cid)):
            bot = _decode(raw_bot)
            if not isinstance(bot, str) or not bot:
                raise ProactiveStateError("群机器人登记状态损坏")
            hash_map[bot] = hashlib.sha1(bot.encode("utf-8")).hexdigest()
            rank_map[bot] = hashlib.sha1(
                f"{plan['seed']}:{slot_index}:{bot}".encode("utf-8")
            ).hexdigest()
            for raw_instance in self.redis.smembers(self._heartbeat_index_key(corp, bot)):
                instance = _decode(raw_instance)
                if not isinstance(instance, str) or not instance:
                    raise ProactiveStateError("机器人实例登记状态损坏")
                hash_map[instance] = hashlib.sha1(instance.encode("utf-8")).hexdigest()
        return (
            json.dumps(hash_map, ensure_ascii=False, separators=(",", ":")),
            json.dumps(rank_map, ensure_ascii=False, separators=(",", ":")),
        )

    def claim(
        self,
        corp: str,
        cid: str,
        day: date | datetime | str,
        slot_index: int,
        bot: str,
        now: Any,
    ) -> str | None:
        epoch = self.ensure_epoch(corp)
        if isinstance(slot_index, bool) or not isinstance(slot_index, int):
            return None
        bot = self._validate_bot(bot)
        self._ensure_robot_pin(bot)
        day_value = _day_text(day)
        current = _epoch(now)
        token = uuid.uuid4().hex
        hash_map, rank_map = self._claim_hash_maps(corp, cid, day_value, slot_index)
        result = self.redis.eval(
            _CLAIM_LUA,
            8,
            self._plan_key(corp, cid, day_value),
            self._daily_state_key(corp, cid, day_value),
            self._lease_key(corp, cid),
            self._heartbeat_key(corp, bot, self.instance_id),
            self._group_member_key(corp, cid, bot),
            self._last_speaker_key(corp, cid),
            self._group_index_key(corp, cid),
            self._epoch_key(corp),
            day_value,
            str(slot_index),
            bot,
            str(current),
            token,
            str(LEASE_TTL_SECONDS),
            str(COOLDOWN_SECONDS),
            str(GRACE_SECONDS),
            str(PLAN_TTL_SECONDS),
            str(HEARTBEAT_TTL_SECONDS),
            str(GROUP_MEMBER_TTL_SECONDS),
            _PREFIX,
            _digest(corp),
            _digest(cid),
            hash_map,
            rank_map,
            epoch,
        )
        code, detail = self._claim_result(result)
        if code == 1:
            if not detail:
                raise ProactiveStateError("主动抢占令牌为空")
            return detail
        if detail in {"missing-plan", "invalid-state", "epoch", "state"}:
            raise ProactiveStateError(f"主动抢占状态不可用: {detail}")
        if detail not in {"bounds", "window", "offline", "leased", "consumed", "cooldown", "winner"}:
            raise ProactiveStateError(f"主动抢占原因未知: {detail}")
        return None

    def owns(self, corp: str, cid: str, token: str) -> bool:
        epoch = self.ensure_epoch(corp)
        if not token:
            return False
        result = self.redis.eval(
            _OWNS_LUA,
            2,
            self._lease_key(corp, cid),
            self._epoch_key(corp),
            token,
            epoch,
        )
        try:
            checked = int(_decode(result))
            if checked == -1:
                raise ProactiveStateError("主动 epoch 在租约校验期间变化")
            return checked == 1
        except (TypeError, ValueError) as exc:
            raise ProactiveStateError("主动租约所有权返回状态损坏") from exc

    def finish(self, corp: str, cid: str, token: str) -> bool:
        epoch = self.ensure_epoch(corp)
        if not token:
            return False
        result = self.redis.eval(
            _FINISH_LUA,
            2,
            self._lease_key(corp, cid),
            self._epoch_key(corp),
            token,
            epoch,
        )
        try:
            checked = int(_decode(result))
            if checked == -1:
                raise ProactiveStateError("主动 epoch 在租约释放期间变化")
            return checked == 1
        except (TypeError, ValueError) as exc:
            raise ProactiveStateError("主动租约释放返回状态损坏") from exc


__all__ = [
    "COOLDOWN_SECONDS",
    "GRACE_SECONDS",
    "GROUP_MEMBER_TTL_SECONDS",
    "HEARTBEAT_TTL_SECONDS",
    "LEASE_TTL_SECONDS",
    "PLAN_TTL_SECONDS",
    "ProactiveStateError",
    "ProactiveStore",
]
