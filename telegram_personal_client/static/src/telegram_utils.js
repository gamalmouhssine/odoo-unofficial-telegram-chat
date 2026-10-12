import { deserializeDateTime } from "@web/core/l10n/dates";

const { DateTime } = luxon;

const AVATAR_COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"];

/** Time for a chat list row: hour today, weekday this week, date otherwise. */
export function formatListDate(value) {
    if (!value) {
        return "";
    }
    const date = deserializeDateTime(value);
    const now = DateTime.now();
    if (date.hasSame(now, "day")) {
        return date.toLocaleString(DateTime.TIME_SIMPLE);
    }
    if (now.diff(date, "days").days < 7) {
        return date.toFormat("ccc");
    }
    return date.toLocaleString(DateTime.DATE_SHORT);
}

export function formatTime(value) {
    return value ? deserializeDateTime(value).toLocaleString(DateTime.TIME_SIMPLE) : "";
}

export function formatDay(value) {
    const date = deserializeDateTime(value);
    return date.hasSame(DateTime.now(), "day") ? null : date.toLocaleString(DateTime.DATE_MED);
}

export function dayKey(value) {
    return deserializeDateTime(value).toISODate();
}

/** Like Telegram: first letters of the first two words ("Site Team – Phase 2" → "ST"). */
export function initials(title) {
    const words = (title || "").trim().split(/\s+/).filter((word) => /^\p{L}/u.test(word));
    if (!words.length) {
        return (title || "?").trim().charAt(0).toUpperCase() || "?";
    }
    return words
        .slice(0, 2)
        .map((word) => [...word][0])
        .join("")
        .toUpperCase();
}

export function avatarColor(id) {
    return AVATAR_COLORS[Math.abs(id || 0) % AVATAR_COLORS.length];
}

export function avatarUrl(chat) {
    return `/web/image/telegram.chat/${chat.id}/avatar_128?unique=${encodeURIComponent(chat.avatar_unique || "")}`;
}

/** Order of a conversation: Telegram date, then Telegram id, then Odoo id. */
export function compareMessages(a, b) {
    if (a.date !== b.date) {
        return a.date < b.date ? -1 : 1;
    }
    if (a.sort_key !== b.sort_key) {
        // Pending messages (no Telegram id yet) stay after sent ones of the same second.
        return (a.sort_key || Infinity) - (b.sort_key || Infinity);
    }
    return a.id - b.id;
}

/** Chat list order: pinned first, then most recent message. */
export function compareChats(a, b) {
    if (a.is_pinned !== b.is_pinned) {
        return a.is_pinned ? -1 : 1;
    }
    const left = a.last_message_date || "";
    const right = b.last_message_date || "";
    if (left !== right) {
        return left < right ? 1 : -1;
    }
    return b.id - a.id;
}

export function newLocalId() {
    if (window.crypto?.randomUUID) {
        return window.crypto.randomUUID();
    }
    return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 14)}`;
}

export const CONTENT_ICONS = {
    photo: "fa-picture-o",
    video: "fa-video-camera",
    document: "fa-file-o",
    voice: "fa-microphone",
    sticker: "fa-smile-o",
    other: "fa-paperclip",
};
