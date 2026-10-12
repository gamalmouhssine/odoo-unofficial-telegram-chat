import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useDebounced } from "@web/core/utils/timing";
import { avatarColor, initials } from "../../telegram_utils";

/** Telegram address book: pick a contact to open (or start) a private chat. */
export class ContactPanel extends Component {
    static template = "telegram_personal_client.ContactPanel";
    static props = { onOpen: Function, onClose: Function, openingId: { type: Number, optional: true } };

    setup() {
        this.state = useState({ search: "", contacts: [], loading: true });
        this.avatarColor = avatarColor;
        this.initials = initials;
        this.debouncedLoad = useDebounced(() => this.load(), 300);
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        const result = await rpc("/telegram/contacts", { search: this.state.search });
        this.state.contacts = result.contacts;
        this.state.loading = false;
    }

    onSearch() {
        this.debouncedLoad();
    }
}
