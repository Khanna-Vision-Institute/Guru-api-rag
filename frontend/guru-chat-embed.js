/**
 * Guru AI Chat Widget Embed Script
 * Include this script on your website to add the Guru AI chat widget
 *
 * Usage:
 * <script src="guru-chat-embed.js"></script>
 *
 * Or inline:
 * <script>
 *   // Copy the contents of this file here
 * </script>
 */

(function() {
    // Configuration
    const CONFIG = {
        apiUrl: 'http://ec2-100-28-122-42.compute-1.amazonaws.com:8000', // Update for production
        primaryColor: '#4f46e5',
        secondaryColor: '#7c3aed',
        position: 'bottom-right', // 'bottom-right', 'bottom-left', 'top-right', 'top-left'
        widgetSize: { width: 400, height: 600 }
    };

    // Create and inject CSS
    function injectStyles() {
        const style = document.createElement('style');
        style.textContent = `
            .guru-chat-widget {
                position: fixed;
                z-index: 9999;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            }

            .guru-chat-widget.bottom-right {
                bottom: 20px;
                right: 20px;
            }

            .guru-chat-widget.bottom-left {
                bottom: 20px;
                left: 20px;
            }

            .guru-chat-widget.top-right {
                top: 20px;
                right: 20px;
            }

            .guru-chat-widget.top-left {
                top: 20px;
                left: 20px;
            }

            .guru-chat-toggle {
                width: 60px;
                height: 60px;
                border-radius: 50%;
                background: linear-gradient(135deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
                border: none;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                transition: all 0.3s ease;
                color: white;
                font-size: 24px;
            }

            .guru-chat-toggle:hover {
                transform: scale(1.05);
                box-shadow: 0 6px 20px rgba(0,0,0,0.2);
            }

            .guru-chat-container {
                position: absolute;
                bottom: 80px;
                right: 0;
                width: ${CONFIG.widgetSize.width}px;
                height: ${CONFIG.widgetSize.height}px;
                background: white;
                border-radius: 20px;
                box-shadow: 0 20px 40px rgba(0,0,0,0.15);
                display: none;
                flex-direction: column;
                overflow: hidden;
                opacity: 0;
                transform: translateY(20px) scale(0.95);
                transition: all 0.3s ease;
            }

            .guru-chat-container.open {
                display: flex;
                opacity: 1;
                transform: translateY(0) scale(1);
            }

            .guru-chat-header {
                background: linear-gradient(135deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
                color: white;
                padding: 20px;
                text-align: center;
                position: relative;
            }

            .guru-chat-header h2 {
                font-size: 18px;
                font-weight: 600;
                margin-bottom: 4px;
            }

            .guru-chat-header p {
                font-size: 14px;
                opacity: 0.9;
            }

            .guru-chat-close {
                position: absolute;
                top: 10px;
                right: 15px;
                background: none;
                border: none;
                color: white;
                font-size: 24px;
                cursor: pointer;
                padding: 5px;
                opacity: 0.8;
                transition: opacity 0.2s;
            }

            .guru-chat-close:hover {
                opacity: 1;
            }

            .guru-chat-messages {
                flex: 1;
                padding: 20px;
                overflow-y: auto;
                background: #f8fafc;
            }

            .guru-message {
                margin-bottom: 16px;
                padding: 12px 16px;
                border-radius: 18px;
                max-width: 80%;
                animation: guruFadeIn 0.3s ease-in;
            }

            .guru-message.user {
                background: linear-gradient(135deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
                color: white;
                margin-left: auto;
                border-bottom-right-radius: 4px;
            }

            .guru-message.bot {
                background: white;
                color: #374151;
                border-bottom-left-radius: 4px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            }

            .guru-message.typing {
                background: white;
                color: #6b7280;
                font-style: italic;
            }

            .guru-chat-input-container {
                padding: 20px;
                background: white;
                border-top: 1px solid #e5e7eb;
            }

            .guru-chat-input {
                display: flex;
                gap: 12px;
            }

            .guru-chat-input input {
                flex: 1;
                padding: 12px 16px;
                border: 2px solid #e5e7eb;
                border-radius: 25px;
                font-size: 16px;
                outline: none;
                transition: border-color 0.2s;
            }

            .guru-chat-input input:focus {
                border-color: ${CONFIG.primaryColor};
            }

            .guru-chat-input button {
                width: 48px;
                height: 48px;
                border: none;
                border-radius: 50%;
                background: linear-gradient(135deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
                color: white;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: transform 0.2s, box-shadow 0.2s;
            }

            .guru-chat-input button:hover:not(:disabled) {
                transform: scale(1.05);
                box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
            }

            .guru-chat-input button:disabled {
                opacity: 0.6;
                cursor: not-allowed;
                transform: none;
            }

            @keyframes guruFadeIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }

            .guru-loading-dots {
                display: inline-block;
            }

            .guru-loading-dots::after {
                content: '';
                animation: guruLoading 1.5s infinite;
            }

            @keyframes guruLoading {
                0%, 20% { content: ''; }
                40% { content: '.'; }
                60% { content: '..'; }
                80%, 100% { content: '...'; }
            }

            @media (max-width: 480px) {
                .guru-chat-container {
                    width: calc(100vw - 40px) !important;
                    height: calc(100vh - 120px) !important;
                    bottom: 80px !important;
                    right: 20px !important;
                }

                .guru-chat-widget.bottom-right {
                    right: 10px !important;
                    bottom: 10px !important;
                }
            }
        `;
        document.head.appendChild(style);
    }

    // Create chat widget HTML
    function createWidget() {
        const widget = document.createElement('div');
        widget.className = `guru-chat-widget ${CONFIG.position}`;

        widget.innerHTML = `
            <button class="guru-chat-toggle" id="guruChatToggle">
                🤖
            </button>
            <div class="guru-chat-container" id="guruChatContainer">
                <div class="guru-chat-header">
                    <button class="guru-chat-close" id="guruChatClose">&times;</button>
                    <h2>🤖 Guru AI</h2>
                    <p>Your medical assistant</p>
                </div>
                <div class="guru-chat-messages" id="guruChatMessages">
                    <div class="guru-message bot">
                        Hello! I'm Guru, your AI medical assistant. I can help answer questions about LASIK surgery and vision correction procedures at Khanna Institute. How can I assist you today?
                    </div>
                </div>
                <div class="guru-chat-input-container">
                    <div class="guru-chat-input">
                        <input type="text" id="guruMessageInput" placeholder="Ask me about LASIK, vision correction..." maxlength="500">
                        <button id="guruSendButton">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="22" y1="2" x2="11" y2="13"></line>
                                <polygon points="22,2 15,22 11,13 2,9"></polygon>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        `;

        document.body.appendChild(widget);
        return widget;
    }

    // Initialize chat functionality
    function initChat() {
        const toggle = document.getElementById('guruChatToggle');
        const container = document.getElementById('guruChatContainer');
        const close = document.getElementById('guruChatClose');
        const messages = document.getElementById('guruChatMessages');
        const input = document.getElementById('guruMessageInput');
        const sendButton = document.getElementById('guruSendButton');

        let isOpen = false;
        let isLoading = false;

        function toggleChat() {
            isOpen = !isOpen;
            if (isOpen) {
                container.classList.add('open');
                input.focus();
            } else {
                container.classList.remove('open');
            }
        }

        function addMessage(text, type, isSmall = false) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `guru-message ${type}`;
            if (isSmall) {
                messageDiv.style.fontSize = '12px';
                messageDiv.style.opacity = '0.7';
            }
            messageDiv.textContent = text;
            messages.appendChild(messageDiv);
            scrollToBottom();
        }

        function showTyping() {
            isLoading = true;
            sendButton.disabled = true;

            const typingDiv = document.createElement('div');
            typingDiv.className = 'guru-message bot typing';
            typingDiv.id = 'guruTypingIndicator';
            typingDiv.innerHTML = 'Guru is thinking<span class="guru-loading-dots"></span>';
            messages.appendChild(typingDiv);
            scrollToBottom();
        }

        function hideTyping() {
            isLoading = false;
            sendButton.disabled = false;

            const typingIndicator = document.getElementById('guruTypingIndicator');
            if (typingIndicator) {
                typingIndicator.remove();
            }
        }

        function scrollToBottom() {
            messages.scrollTop = messages.scrollHeight;
        }

        async function sendMessage() {
            const message = input.value.trim();
            if (!message || isLoading) return;

            // Add user message
            addMessage(message, 'user');
            input.value = '';

            // Show typing indicator
            showTyping();

            try {
                const response = await callAPI(message);
                hideTyping();
                addMessage(response.answer, 'bot');

                // Optional: Show which model was used
                if (response.model_used !== 'openai') {
                    addMessage(`Powered by ${response.model_used}`, 'bot', true);
                }
            } catch (error) {
                hideTyping();
                addMessage('Sorry, I\'m having trouble connecting right now. Please try again later or contact Khanna Institute directly.', 'bot');
                console.error('Guru API Error:', error);
            }
        }

        async function callAPI(query) {
            const response = await fetch(`${CONFIG.apiUrl}/ask`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    query: query,
                    top_k: 3
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            return await response.json();
        }

        // Event listeners
        toggle.addEventListener('click', toggleChat);
        close.addEventListener('click', toggleChat);

        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !isLoading) {
                sendMessage();
            }
        });

        sendButton.addEventListener('click', sendMessage);

        // Close on outside click
        document.addEventListener('click', (e) => {
            if (isOpen && !widget.contains(e.target)) {
                toggleChat();
            }
        });
    }

    // Initialize when DOM is ready
    function init() {
        injectStyles();
        const widget = createWidget();
        initChat();
    }

    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
