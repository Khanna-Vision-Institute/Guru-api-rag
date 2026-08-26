/**
 * Vapi Assistant Setup Script
 * Creates/updates the Guru AI assistant in Vapi
 * 
 * Usage: node vapi_setup.js
 */

require('dotenv').config();
const axios = require('axios');

const VAPI_API_KEY = process.env.VAPI_API_KEY;
const BACKEND_API_URL = process.env.BACKEND_API_URL || 'http://ec2-100-28-122-42.compute-1.amazonaws.com:8000';
const WEBHOOK_URL = process.env.WEBHOOK_URL || `${BACKEND_API_URL}/vapi/webhook`;

if (!VAPI_API_KEY) {
    console.error('❌ Error: VAPI_API_KEY not found in environment variables');
    console.error('Please add VAPI_API_KEY to your .env file');
    process.exit(1);
}

// Use HTTPS webhook through nginx proxy
const WEBHOOK_URL_HTTPS = process.env.WEBHOOK_URL_HTTPS || 'https://khannainstitute.com/api/guru/vapi/webhook';

const ASSISTANT_CONFIG = {
    name: "Guru",
    model: {
        provider: "openai",
        model: "gpt-4o-mini",
        temperature: 0.2,
        maxTokens: 1024
    },
    voice: {
        provider: "11labs",
        voiceId: "21m00Tcm4TlvDq8ikWAM"
    },
    firstMessage: "Hi! I'm Guru, your AI vision assistant. I can help answer questions about LASIK, EVO ICL, SMILE laser, cataract surgery, and all Khanna Institute vision correction procedures. How can I assist you today?",
    serverUrl: WEBHOOK_URL_HTTPS,
    serverUrlSecret: process.env.WEBHOOK_SECRET || "guru-webhook-secret-2025"
};

async function createAssistant() {
    try {
        console.log('🚀 Creating Guru AI assistant in Vapi...');
        console.log(`📡 Backend URL: ${BACKEND_API_URL}`);
        console.log(`🔗 Webhook URL (HTTPS): ${WEBHOOK_URL_HTTPS}`);

        const response = await axios.post(
            'https://api.vapi.ai/assistant',
            ASSISTANT_CONFIG,
            {
                headers: {
                    'Authorization': `Bearer ${VAPI_API_KEY}`,
                    'Content-Type': 'application/json'
                }
            }
        );

        console.log('✅ Assistant created successfully!');
        console.log('\n📋 Assistant Details:');
        console.log(`   ID: ${response.data.id}`);
        console.log(`   Name: ${response.data.name}`);
        console.log(`   Status: ${response.data.status}`);
        console.log(`   Webhook: ${response.data.serverUrl}`);
        console.log('\n💾 Save this Assistant ID for your website:');
        console.log(`   ASSISTANT_ID=${response.data.id}`);
        console.log('\n📝 Add to your .env file:');
        console.log(`   VAPI_ASSISTANT_ID=${response.data.id}`);

        return response.data;

    } catch (error) {
        if (error.response) {
            console.error('❌ Vapi API Error:');
            console.error(`   Status: ${error.response.status}`);
            console.error(`   Message: ${JSON.stringify(error.response.data, null, 2)}`);
        } else {
            console.error('❌ Error:', error.message);
        }
        throw error;
    }
}

async function updateAssistant(assistantId) {
    try {
        console.log(`🔄 Updating assistant ${assistantId}...`);

        const response = await axios.patch(
            `https://api.vapi.ai/assistant/${assistantId}`,
            ASSISTANT_CONFIG,
            {
                headers: {
                    'Authorization': `Bearer ${VAPI_API_KEY}`,
                    'Content-Type': 'application/json'
                }
            }
        );

        console.log('✅ Assistant updated successfully!');
        return response.data;

    } catch (error) {
        if (error.response) {
            console.error('❌ Vapi API Error:');
            console.error(`   Status: ${error.response.status}`);
            console.error(`   Message: ${JSON.stringify(error.response.data, null, 2)}`);
        } else {
            console.error('❌ Error:', error.message);
        }
        throw error;
    }
}

async function listAssistants() {
    try {
        const response = await axios.get(
            'https://api.vapi.ai/assistant',
            {
                headers: {
                    'Authorization': `Bearer ${VAPI_API_KEY}`
                }
            }
        );

        return response.data;
    } catch (error) {
        console.error('❌ Error listing assistants:', error.message);
        throw error;
    }
}

async function main() {
    const assistantId = process.env.VAPI_ASSISTANT_ID;

    if (assistantId) {
        console.log(`📝 Updating existing assistant: ${assistantId}`);
        await updateAssistant(assistantId);
    } else {
        console.log('🆕 Creating new assistant...');
        const assistants = await listAssistants();
        const existingGuru = assistants.find(a => a.name === 'Guru');
        
        if (existingGuru) {
            console.log(`\n⚠️  Found existing "Guru" assistant (ID: ${existingGuru.id})`);
            console.log('   Updating existing assistant...');
            await updateAssistant(existingGuru.id);
            console.log(`\n💾 Use this ID: VAPI_ASSISTANT_ID=${existingGuru.id}`);
        } else {
            await createAssistant();
        }
    }
}

if (require.main === module) {
    main().catch(error => {
        console.error('\n❌ Setup failed:', error.message);
        process.exit(1);
    });
}

module.exports = { createAssistant, updateAssistant, listAssistants };
