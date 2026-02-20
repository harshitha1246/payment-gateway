(function () {
  class PaymentGateway {
    constructor(options) {
      if (!options || !options.key || !options.orderId) {
        throw new Error("PaymentGateway requires key and orderId");
      }

      this.key = options.key;
      this.orderId = options.orderId;
      this.onSuccess = typeof options.onSuccess === "function" ? options.onSuccess : function () {};
      this.onFailure = typeof options.onFailure === "function" ? options.onFailure : function () {};
      this.onClose = typeof options.onClose === "function" ? options.onClose : function () {};

      this.modalElement = null;
      this.messageHandler = this.handleMessage.bind(this);
    }

    handleMessage(event) {
      if (!event || !event.data || !event.data.type) return;

      if (event.data.type === "payment_success") {
        this.onSuccess(event.data.data || {});
        this.close();
      } else if (event.data.type === "payment_failed") {
        this.onFailure(event.data.data || {});
      } else if (event.data.type === "close_modal") {
        this.close();
      }
    }

    open() {
      if (this.modalElement) return;

      const wrapper = document.createElement("div");
      wrapper.id = "payment-gateway-modal";
      wrapper.setAttribute("data-testid", "payment-modal");
      wrapper.innerHTML =
        '<div class="modal-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:9999;">' +
        '<div class="modal-content" style="position:relative;width:min(480px,95vw);height:min(700px,90vh);background:#fff;border-radius:8px;overflow:hidden;">' +
        '<iframe data-testid="payment-iframe" style="width:100%;height:100%;border:0;" src="http://localhost:3001/checkout?order_id=' +
        encodeURIComponent(this.orderId) +
        '&embedded=true"></iframe>' +
        '<button data-testid="close-modal-button" class="close-button" style="position:absolute;right:8px;top:8px;width:32px;height:32px;border:none;border-radius:16px;background:#eee;cursor:pointer;">×</button>' +
        "</div></div>";

      const closeBtn = wrapper.querySelector('[data-testid="close-modal-button"]');
      closeBtn.addEventListener("click", () => this.close());

      document.body.appendChild(wrapper);
      window.addEventListener("message", this.messageHandler);
      this.modalElement = wrapper;
    }

    close() {
      if (this.modalElement && this.modalElement.parentNode) {
        this.modalElement.parentNode.removeChild(this.modalElement);
      }
      this.modalElement = null;
      window.removeEventListener("message", this.messageHandler);
      this.onClose();
    }
  }

  window.PaymentGateway = PaymentGateway;
})();
