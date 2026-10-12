import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

/**
 * Login steps for the user's own Telegram account: intro and consent, QR code
 * or phone number, code, two-step password. The server decides the step
 * (status.state); this component only renders it and submits values.
 */
export class ConnectWizard extends Component {
    static template = "telegram_personal_client.ConnectWizard";
    static props = { status: Object, onStatus: Function };

    setup() {
        this.form = useState({ value: "", busy: false });
    }

    get step() {
        return this.props.status.state;
    }

    /** Waiting for Telegram: a request is queued or running. */
    get waiting() {
        return this.form.busy || this.props.status.pending;
    }

    /** Where Telegram sent the login code: telegram_app, sms, call, email or other. */
    get codeDestination() {
        return this.props.status.code_delivery || "other";
    }

    start(method) {
        return this.call("/telegram/auth/start", { method });
    }

    submit(step) {
        const value = this.form.value;
        // Never keep codes or passwords in memory longer than needed.
        this.form.value = "";
        return this.call("/telegram/auth/submit", { step, value });
    }

    cancel() {
        return this.call("/telegram/auth/logout", {});
    }

    async call(route, params) {
        this.form.busy = true;
        try {
            this.props.onStatus(await rpc(route, params));
        } finally {
            this.form.busy = false;
        }
    }
}
