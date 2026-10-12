import { Component, onMounted, onPatched, onWillPatch, useRef, useState } from "@odoo/owl";
import {
    CONTENT_ICONS,
    avatarColor,
    avatarUrl,
    dayKey,
    formatDay,
    formatTime,
    initials,
} from "../../telegram_utils";

const LOAD_OLDER_THRESHOLD = 80;
const AT_BOTTOM_THRESHOLD = 60;

/**
 * One open chat: header, messages (oldest at the top, more loaded while
 * scrolling up) and the composer.
 */
export class Conversation extends Component {
    static template = "telegram_personal_client.Conversation";
    static props = {
        chat: Object,
        entry: Object,
        onSend: Function,
        onLoadOlder: Function,
        onRetry: Function,
        onBack: Function,
    };

    setup() {
        this.listRef = useRef("list");
        this.inputRef = useRef("input");
        this.composer = useState({ text: "" });
        this.CONTENT_ICONS = CONTENT_ICONS;
        this.avatarUrl = avatarUrl;
        this.avatarColor = avatarColor;
        this.initials = initials;
        this.formatTime = formatTime;
        onMounted(() => {
            this.scrollToBottom();
            this.rememberDisplayed();
            this.inputRef.el?.focus();
        });
        // Snapshot of the DOM still on screen, taken before OWL patches it.
        onWillPatch(() => {
            const list = this.listRef.el;
            this.snapshot = list && {
                chatId: this.displayedChatId,
                firstId: this.displayedFirstId,
                height: list.scrollHeight,
                top: list.scrollTop,
                atBottom: list.scrollTop + list.clientHeight >= list.scrollHeight - AT_BOTTOM_THRESHOLD,
            };
        });
        onPatched(() => {
            const list = this.listRef.el;
            const snapshot = this.snapshot;
            if (!list || !snapshot || snapshot.chatId !== this.props.chat.id) {
                this.scrollToBottom();
            } else if (snapshot.firstId !== this.firstId) {
                // Older messages were added above: keep the visible ones in place.
                list.scrollTop = list.scrollHeight - snapshot.height + snapshot.top;
            } else if (snapshot.atBottom) {
                this.scrollToBottom();
            }
            this.rememberDisplayed();
        });
    }

    get firstId() {
        const items = this.props.entry.items;
        return items.length ? items[0].id : null;
    }

    rememberDisplayed() {
        this.displayedChatId = this.props.chat.id;
        this.displayedFirstId = this.firstId;
    }

    get rows() {
        const items = this.props.entry.items;
        const rows = [];
        let previousDay = null;
        let previousSender = null;
        for (const message of items) {
            const day = dayKey(message.date);
            if (day !== previousDay) {
                rows.push({ key: `day-${day}`, type: "day", label: formatDay(message.date) });
                previousDay = day;
                previousSender = null;
            }
            const sender = message.is_outgoing ? "__me__" : message.sender_name;
            rows.push({
                key: `message-${message.id}`,
                type: "message",
                message,
                showSender: this.isGroup && !message.is_outgoing && sender !== previousSender,
            });
            previousSender = sender;
        }
        return rows;
    }

    get isGroup() {
        return this.props.chat.chat_type !== "private";
    }

    isRead(message) {
        return message.state === "sent" && message.sort_key && message.sort_key <= this.props.chat.last_read_outbox_key;
    }

    scrollToBottom() {
        const list = this.listRef.el;
        if (list) {
            list.scrollTop = list.scrollHeight;
        }
    }

    onScroll() {
        const list = this.listRef.el;
        if (list && list.scrollTop < LOAD_OLDER_THRESHOLD) {
            this.props.onLoadOlder();
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
            ev.preventDefault();
            this.send();
        }
    }

    send() {
        const text = this.composer.text;
        if (!text.trim() || !this.props.chat.can_send) {
            return;
        }
        this.composer.text = "";
        this.props.onSend(text);
        this.inputRef.el?.focus();
    }
}
