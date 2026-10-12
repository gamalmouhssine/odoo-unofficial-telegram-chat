"""Conversion of Telethon objects into plain rows stored by the models.

Rows are plain dicts (JSON-friendly) so the engine can re-apply them after a
failed commit, and so this file is the only place that knows Telethon types.
"""
import datetime

from .vendor import ensure_libraries

ensure_libraries()
from telethon import types, utils  # noqa: E402

# Raw updates the engine listens to for connected accounts.
SYNC_UPDATES = (
    types.UpdateNewMessage, types.UpdateNewChannelMessage,
    types.UpdateEditMessage, types.UpdateEditChannelMessage,
    types.UpdateDeleteMessages, types.UpdateDeleteChannelMessages,
    types.UpdateReadHistoryInbox, types.UpdateReadChannelInbox,
    types.UpdateReadHistoryOutbox, types.UpdateReadChannelOutbox,
)

_MEDIA_LABELS = {
    types.MessageMediaContact: 'Contact',
    types.MessageMediaGeo: 'Location',
    types.MessageMediaGeoLive: 'Live location',
    types.MessageMediaVenue: 'Location',
    types.MessageMediaPoll: 'Poll',
    types.MessageMediaDice: 'Dice',
    types.MessageMediaGame: 'Game',
    types.MessageMediaInvoice: 'Invoice',
}


def peer_key(peer):
    """Telegram's "marked" id as text: users > 0, groups < 0, supergroups -100…"""
    return str(utils.get_peer_id(peer))


def naive_utc(value):
    if isinstance(value, datetime.datetime) and value.tzinfo:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def has_photo(entity):
    return isinstance(getattr(entity, 'photo', None), (types.UserProfilePhoto, types.ChatPhoto))


def _send_banned(rights):
    return bool(rights and rights.send_messages)


def chat_row(entity, dialog=None):
    """Row for telegram.chat, or None for chats the module does not show
    (broadcast channels: ToS 3.3 requires sponsored messages there)."""
    if isinstance(entity, types.User):
        if entity.is_self:
            title = 'Saved Messages'
        else:
            title = utils.get_display_name(entity) or ('Deleted Account' if entity.deleted else (entity.phone or ''))
        row = {'chat_type': 'private', 'can_send': not entity.deleted,
               'contact_tg_user_id': str(entity.id)}
    elif isinstance(entity, types.Chat):
        title = entity.title
        row = {'chat_type': 'basic_group', 'can_send': not (
            entity.left or entity.deactivated or entity.migrated_to or _send_banned(entity.default_banned_rights))}
    elif isinstance(entity, types.Channel) and not entity.broadcast:
        title = entity.title
        privileged = bool(entity.creator or entity.admin_rights)
        banned = _send_banned(entity.default_banned_rights) or _send_banned(entity.banned_rights)
        row = {'chat_type': 'supergroup', 'can_send': not entity.left and (privileged or not banned)}
    else:
        return None
    row.update({'tg_chat_id': peer_key(entity), 'title': title or '', 'photo_available': has_photo(entity)})
    if dialog is not None:
        raw = dialog.dialog
        row.update({
            'unread_count': dialog.unread_count or 0,
            'is_pinned': bool(dialog.pinned),
            'last_read_inbox_key': float(raw.read_inbox_max_id or 0),
            'last_read_outbox_key': float(raw.read_outbox_max_id or 0),
        })
    return row


def contact_row(user):
    usernames = [u.username for u in (user.usernames or []) if u.active]
    return {
        'tg_user_id': str(user.id),
        'first_name': user.first_name or '',
        'last_name': user.last_name or '',
        'username': user.username or (usernames[0] if usernames else ''),
        'phone': user.phone or '',
        'is_contact': bool(user.contact),
    }


def content_kind(media):
    """(content_type, label) for a message's media; label is shown in previews."""
    if media is None or isinstance(media, (types.MessageMediaEmpty, types.MessageMediaWebPage)):
        return 'text', ''
    if isinstance(media, types.MessageMediaPhoto):
        return 'photo', 'Photo'
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        attributes = media.document.attributes or []
        for attribute in attributes:
            if isinstance(attribute, types.DocumentAttributeSticker):
                return 'sticker', 'Sticker'
            if isinstance(attribute, types.DocumentAttributeAudio) and attribute.voice:
                return 'voice', 'Voice message'
            if isinstance(attribute, types.DocumentAttributeVideo):
                return 'video', 'Video message' if attribute.round_message else 'Video'
            if isinstance(attribute, types.DocumentAttributeAnimated):
                return 'other', 'GIF'
        for attribute in attributes:
            if isinstance(attribute, types.DocumentAttributeFilename):
                return 'document', attribute.file_name or 'File'
        return 'document', 'File'
    return 'other', _MEDIA_LABELS.get(type(media), 'Message')


def message_row(message, tg_chat_id, sender_name):
    content_type, label = content_kind(message.media)
    return {
        'tg_chat_id': tg_chat_id,
        'tg_message_id': str(message.id),
        'sort_key': float(message.id),
        'date': naive_utc(message.date),
        'is_outgoing': bool(message.out),
        'sender_tg_id': peer_key(message.from_id) if message.from_id else False,
        'sender_name': sender_name or False,
        'body': message.message or '',
        'content_type': content_type,
        'content_meta': {'label': label} if label else False,
        'is_edited': bool(message.edit_date) and not message.edit_hide,
        'state': 'sent' if message.out else 'received',
    }
