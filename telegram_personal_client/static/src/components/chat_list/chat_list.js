import { Component } from "@odoo/owl";
import { avatarColor, avatarUrl, formatListDate, initials } from "../../telegram_utils";

export class ChatList extends Component {
    static template = "telegram_personal_client.ChatList";
    static props = { chats: Array, selectedId: { type: Number, optional: true }, onSelect: Function };

    avatarUrl = avatarUrl;
    avatarColor = avatarColor;
    initials = initials;
    formatListDate = formatListDate;
}
