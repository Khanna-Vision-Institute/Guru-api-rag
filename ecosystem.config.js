module.exports = {
  apps: [{
    name: 'guru-rag-api',
    script: 'main.py',
    interpreter: 'python3',
    cwd: '/home/ubuntu/guru_rag',
    instances: 1,
    exec_mode: 'fork',
    env: {
      PYTHONPATH: '/home/ubuntu/guru_rag'
    },
    error_log: '/home/ubuntu/guru_rag/logs/pm2-error.log',
    out_log: '/home/ubuntu/guru_rag/logs/pm2-out.log',
    log_log: '/home/ubuntu/guru_rag/logs/pm2-combined.log',
    time: true,
    watch: false,
    max_memory_restart: '1G',
    restart_delay: 4000,
    autorestart: true,
    env_production: {
      NODE_ENV: 'production'
    }
  }]
};
