"""Group-scoped membership and explicit grants. Unknown users are denied."""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from .database.models import TelegramGroupAccess, TelegramUser, TelegramTarget, AclRule
from .auth.pairing import DEFAULT_PERMISSIONS
from .domain.enums import Role


def target_ready(target):
    return bool(target and target.is_active and target.validated_at and target.missing_permissions in (None, '', '[]'))


async def find_target(db, chat_id, account_id=1):
    async with db.session() as s:
        return await s.scalar(select(TelegramTarget).where(TelegramTarget.chat_id==str(chat_id),TelegramTarget.account_id==account_id))


async def can_send(db, target_id, user_id):
    if not target_id or not user_id:
        return False
    async with db.session() as s:
        user=await s.scalar(select(TelegramUser).where(TelegramUser.user_id==str(user_id)))
        grant=await s.scalar(select(TelegramGroupAccess).where(TelegramGroupAccess.target_id==target_id,TelegramGroupAccess.user_id==str(user_id)))
        target=await s.get(TelegramTarget,target_id)
        if not target_ready(target) or not user or user.blocked or not grant or not grant.approved or grant.blocked or grant.membership in ('left','kicked'):
            return False
        try:
            if 'send_message' not in DEFAULT_PERMISSIONS.get(Role(grant.role),set()):return False
        except ValueError:return False
        # Preserve applicable legacy overrides as an additional restriction.
        # An old allow rule can never create a missing per-group grant.
        rules=(await s.scalars(select(AclRule).where(AclRule.principal==str(user_id),AclRule.permission=='send_message'))).all()
        applicable=[r for r in rules if r.scope=='global' or
            (r.scope in {'user','chat'} and r.scope_ref==target.chat_id)]
        applicable.sort(key=lambda r:({'user':0,'chat':1,'global':2}[r.scope],r.id))
        return bool(applicable[0].allow) if applicable else True


async def observe(db, target_id, user_id, *, membership=None, is_admin=False, source='message'):
    values=dict(target_id=target_id,user_id=str(user_id),role='read_only',approved=False,blocked=False,
                membership=membership,is_admin=is_admin,source=source)
    updates={'source':source}
    if membership is not None:updates.update(membership=membership,is_admin=is_admin)
    async with db.session() as s:
        await s.execute(insert(TelegramGroupAccess).values(**values).on_conflict_do_update(
            index_elements=['target_id','user_id'],set_=updates))
        await s.commit()
