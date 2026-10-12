import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    useExternalListener,
    useState,
} from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { ChatList } from "../chat_list/chat_list";
import { ConnectWizard } from "../connect_wizard/connect_wizard";
import { ContactPanel } from "../contact_panel/contact_panel";
import { Conversation } from "../conversation/conversation";
import { compareChats, compareMessages, newLocalId } from "../../telegram_utils";

const HEARTBEAT_MS = 30000;
// Fallback refresh while a request waits for Telegram, in case a bus
// notification is missed (e.g. the websocket reconnected).
const PENDING_REFRESH_MS = 3000;
const PAGE_SIZE = 50;
const REMOTE_HISTORY_TIMEOUT_MS = 15000;

/**
 * Root of the Telegram screen (client action).
 *
 * Shows the login wizard until the account is connected, then the chat list
 * and the open conversation. Changes made by the engine arrive on the bus
 * ("telegram/auth_state", "telegram/update"). While the screen is visible, a
 * heartbeat tells the engine to show the user online.
 */
export class TelegramApp extends Component {
    static template = "telegram_personal_client.TelegramApp";
    static components = { ChatList, ConnectWizard, ContactPanel, Conversation };
    static props = ["*"];

    setup() {
        this.busService = useService("bus_service");
        this.dialog = useService("dialog");
        this.state = useState({
            loading: true,
            status: null,
            chats: [],
            chatsLoaded: false,
            chatsReady: false,
            selectedChatId: undefined,
            entries: {},
            search: "",
            showContacts: false,
            openingContactId: undefined,
        });
        this.onAuthState = () => this.refresh();
        this.onUpdate = (payload) => this.applyUpdate(payload);
        useExternalListener(document, "visibilitychange", () => this.onVisibilityChange());
        onWillStart(() => this.refresh());
        onMounted(() => {
            this.busService.subscribe("telegram/auth_state", this.onAuthState);
            this.busService.subscribe("telegram/update", this.onUpdate);
            this.busService.start();
            this.heartbeatTimer = browser.setInterval(() => this.heartbeat(), HEARTBEAT_MS);
            this.heartbeat();
        });
        onWillUnmount(() => {
            this.busService.unsubscribe("telegram/auth_state", this.onAuthState);
            this.busService.unsubscribe("telegram/update", this.onUpdate);
            browser.clearInterval(this.heartbeatTimer);
            browser.clearTimeout(this.pendingTimer);
        });
    }

    // Account ------------------------------------------------------------------------

    get connected() {
        return this.state.status?.state === "connected";
    }

    async refresh() {
        this.setStatus(await rpc("/telegram/auth/status"));
    }

    setStatus(status) {
        this.state.status = status;
        this.state.loading = false;
        browser.clearTimeout(this.pendingTimer);
        if (status.pending) {
            this.pendingTimer = browser.setTimeout(() => this.refresh(), PENDING_REFRESH_MS);
        }
        if (status.state === "connected" && !this.state.chatsLoaded) {
            this.loadChats();
        } else if (status.state !== "connected") {
            Object.assign(this.state, {
                chats: [], chatsLoaded: false, chatsReady: false, entries: {}, selectedChatId: undefined,
            });
        }
    }

    heartbeat() {
        if (this.connected && document.visibilityState === "visible") {
            rpc("/telegram/heartbeat", {}, { silent: true });
        }
    }

    onVisibilityChange() {
        this.heartbeat();
        if (document.visibilityState === "visible" && this.state.selectedChatId) {
            this.markRead(this.state.selectedChatId);
        }
    }

    confirmDisconnect() {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Disconnect Telegram"),
            body: _t(
                "Odoo will be logged out of your Telegram account and the chats cached in Odoo will be deleted. Your messages stay in Telegram."
            ),
            confirmLabel: _t("Disconnect"),
            confirm: async () => this.setStatus(await rpc("/telegram/auth/logout")),
            cancel: () => {},
        });
    }

    // Chats -------------------------------------------------------------------------

    async loadChats() {
        this.state.chatsLoaded = true;
        const result = await rpc("/telegram/chats");
        this.state.chats = result.chats.sort(compareChats);
        this.state.chatsReady = true;
    }

    get visibleChats() {
        const search = this.state.search.trim().toLowerCase();
        if (!search) {
            return this.state.chats;
        }
        return this.state.chats.filter(
            (chat) =>
                chat.title.toLowerCase().includes(search) ||
                chat.last_message_preview.toLowerCase().includes(search)
        );
    }

    get selectedChat() {
        return this.state.chats.find((chat) => chat.id === this.state.selectedChatId);
    }

    get selectedEntry() {
        return this.state.entries[this.state.selectedChatId];
    }

    async selectChat(chatId) {
        this.state.selectedChatId = chatId;
        this.state.showContacts = false;
        if (!this.state.entries[chatId]) {
            await this.loadLatest(chatId);
        }
        this.markRead(chatId);
    }

    backToList() {
        this.state.selectedChatId = undefined;
    }

    markRead(chatId) {
        const chat = this.state.chats.find((c) => c.id === chatId);
        if (chat?.unread_count && document.visibilityState === "visible") {
            rpc("/telegram/read", { chat_id: chatId }, { silent: true });
        }
    }

    // Messages ----------------------------------------------------------------------

    async loadLatest(chatId) {
        this.state.entries[chatId] = { items: [], hasMore: false, remoteComplete: false, loadingOlder: true };
        // Read back through the state: mutations must go through the reactive proxy.
        const entry = this.state.entries[chatId];
        const result = await rpc("/telegram/messages", { chat_id: chatId, limit: PAGE_SIZE });
        Object.assign(entry, {
            items: result.messages,
            hasMore: result.has_more,
            remoteComplete: result.history_complete,
            loadingOlder: false,
        });
        if (!result.history_loaded && !result.history_complete && result.messages.length < PAGE_SIZE) {
            this.requestRemoteHistory(chatId);
        }
    }

    async loadOlder() {
        const chatId = this.state.selectedChatId;
        const entry = this.state.entries[chatId];
        if (!entry || entry.loadingOlder) {
            return;
        }
        if (entry.hasMore && entry.items.length) {
            entry.loadingOlder = true;
            const oldest = entry.items[0];
            const result = await rpc("/telegram/messages", {
                chat_id: chatId,
                limit: PAGE_SIZE,
                before: { date: oldest.date, id: oldest.id },
            });
            entry.items = [...result.messages, ...entry.items];
            entry.hasMore = result.has_more;
            entry.remoteComplete = result.history_complete;
            entry.loadingOlder = false;
        } else if (!entry.remoteComplete) {
            this.requestRemoteHistory(chatId);
        }
    }

    requestRemoteHistory(chatId) {
        const entry = this.state.entries[chatId];
        entry.loadingOlder = true;
        rpc("/telegram/history", { chat_id: chatId }, { silent: true });
        browser.clearTimeout(entry.historyTimer);
        entry.historyTimer = browser.setTimeout(() => (entry.loadingOlder = false), REMOTE_HISTORY_TIMEOUT_MS);
    }

    async onHistoryLoaded(chatId) {
        const entry = this.state.entries[chatId];
        if (!entry) {
            return;
        }
        browser.clearTimeout(entry.historyTimer);
        entry.loadingOlder = false;
        if (!entry.items.length) {
            await this.loadLatest(chatId);
        } else {
            entry.hasMore = true;
            await this.loadOlder();
        }
    }

    async send(text) {
        const chatId = this.state.selectedChatId;
        const result = await rpc("/telegram/send", { chat_id: chatId, text, local_uuid: newLocalId() });
        this.upsertMessages([result.message]);
    }

    async retry(messageId) {
        const result = await rpc("/telegram/retry", { message_id: messageId });
        this.upsertMessages([result.message]);
    }

    // Contacts ---------------------------------------------------------------------

    async openContact(contactId) {
        this.state.openingContactId = contactId;
        const result = await rpc("/telegram/open_contact", { contact_id: contactId });
        if (result.chat_id) {
            this.state.openingContactId = undefined;
            await this.selectChat(result.chat_id);
        }
        // Otherwise the engine creates the chat and announces it ("open_chat").
    }

    // Live updates -------------------------------------------------------------------

    applyUpdate(payload) {
        if (payload.chats?.length) {
            this.upsertChats(payload.chats);
        }
        if (payload.messages?.length) {
            this.upsertMessages(payload.messages);
        }
        for (const { chat_id, id } of payload.deleted || []) {
            const entry = this.state.entries[chat_id];
            if (entry) {
                entry.items = entry.items.filter((message) => message.id !== id);
            }
        }
        if (payload.history) {
            this.onHistoryLoaded(payload.history);
        }
        if (payload.open_chat && this.state.openingContactId) {
            this.state.openingContactId = undefined;
            this.selectChat(payload.open_chat);
        }
    }

    upsertChats(chats) {
        const byId = new Map(this.state.chats.map((chat) => [chat.id, chat]));
        for (const chat of chats) {
            byId.set(chat.id, chat);
        }
        this.state.chats = [...byId.values()].sort(compareChats);
        if (this.state.selectedChatId && chats.some((chat) => chat.id === this.state.selectedChatId)) {
            this.markRead(this.state.selectedChatId);
        }
    }

    upsertMessages(messages) {
        for (const message of messages) {
            const entry = this.state.entries[message.chat_id];
            if (!entry) {
                continue;
            }
            const index = entry.items.findIndex((item) => item.id === message.id);
            if (index >= 0) {
                entry.items[index] = message;
            } else {
                entry.items = [...entry.items, message].sort(compareMessages);
            }
        }
    }
}

registry.category("actions").add("telegram_personal_client.client", TelegramApp);
