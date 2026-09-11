// ---------------------------------------------------------------------------
// VARIABLES GLOBALES
// ---------------------------------------------------------------------------
let logPosition    = 0;
let logInterval    = null;
let statusInterval = null;
let isRunning      = false;

// ---------------------------------------------------------------------------
// INICIALIZACIÓN
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    // Esperamos 500ms para que el bridge Eel/Python esté listo
    setTimeout(async () => {
        try {
            await cargarConfiguracion();
            iniciarLogReader();
            await checkStatus();
            statusInterval = setInterval(checkStatus, 3000);
        } catch (err) {
            console.error("Error en la inicialización:", err);
        }
    }, 500);
});

// ---------------------------------------------------------------------------
// UI — toggles de tarjetas y campos de hipervisor
// ---------------------------------------------------------------------------
function toggleCard(bodyId, checkbox) {
    const el = document.getElementById(bodyId);
    if (!el) return;
    el.style.opacity       = checkbox.checked ? '1'    : '0.5';
    el.style.pointerEvents = checkbox.checked ? 'auto' : 'none';

    // El autoenrute DICOM y los KPIs de RIS vía Elastic leen desde
    // ElasticSearch: si se apaga esa tarjeta, ambos sub-ítems quedan sin
    // fuente de datos y se desactivan.
    if (bodyId === 'elastic_body' && !checkbox.checked) {
        const dicomRoutingSwitch = document.getElementById('enabled_dicom_routing');
        if (dicomRoutingSwitch) {
            dicomRoutingSwitch.checked = false;
            toggleDicomRouting(dicomRoutingSwitch);
        }
        const risMetricsSwitch = document.getElementById('enabled_ris_metrics');
        if (risMetricsSwitch) {
            risMetricsSwitch.checked = false;
            toggleRisMetrics(risMetricsSwitch);
        }
    }
}

// Habilita/deshabilita los campos propios del sub-ítem de autoenrute.
function toggleDicomRouting(checkbox) {
    const el = document.getElementById('dicom_routing_fields');
    if (!el) return;
    el.style.opacity       = checkbox.checked ? '1'    : '0.5';
    el.style.pointerEvents = checkbox.checked ? 'auto' : 'none';
}

// Habilita/deshabilita los campos propios del sub-ítem de KPIs de RIS vía Elastic.
function toggleRisMetrics(checkbox) {
    const el = document.getElementById('ris_metrics_fields');
    if (!el) return;
    el.style.opacity       = checkbox.checked ? '1'    : '0.5';
    el.style.pointerEvents = checkbox.checked ? 'auto' : 'none';
}

function toggleHypervisorFields() {
    const type          = document.getElementById('hyper_type').value;
    const nodeContainer = document.getElementById('px_node_container');
    const hint          = document.getElementById('hyper_hint');
    const vmwareNote    = document.getElementById('vmware_note');

    if (type === 'vmware') {
        nodeContainer.style.display = 'none';
        document.getElementById('px_node').value = '';
        hint.innerText = "Conexión directa a vCenter o ESXi (Puerto 443). Requiere pyVmomi instalado.";
        vmwareNote.style.display = 'block';
    } else {
        nodeContainer.style.display = 'block';
        hint.innerText = "Requiere IP, Usuario, Password y el Nombre exacto del Nodo en el Cluster.";
        vmwareNote.style.display = 'none';
    }
}

// ---------------------------------------------------------------------------
// BLOQUEO VISUAL DEL BOTÓN DESPUÉS DE GUARDAR
// ---------------------------------------------------------------------------
function bloquearBotonMonitoreo(segundos) {
    const btn = document.getElementById('btn_monitor_toggle');
    btn.disabled   = true;
    btn.className  = 'btn btn-secondary btn-lg';

    let s = segundos;
    btn.innerHTML = `<i class="fas fa-spinner fa-spin me-2"></i>Aplicando (${s}s)...`;

    const tick = setInterval(() => {
        s--;
        if (s > 0) {
            btn.innerHTML = `<i class="fas fa-spinner fa-spin me-2"></i>Aplicando (${s}s)...`;
        }
    }, 1000);

    setTimeout(async () => {
        clearInterval(tick);
        btn.disabled = false;
        await checkStatus();
    }, segundos * 1000);
}

async function cargarConfiguracion() {
    try {
        const cfg = await eel.cargar_config()();
        if (!cfg || Object.keys(cfg).length === 0 || cfg._error) {
            console.warn("Configuración vacía o con error:", cfg?._error);
            return;
        }

        // --- 1. Configuración Central ---
        document.getElementById('hosp_id').value     = cfg.hospital_id || '';
        document.getElementById('auth_token').value  = cfg.auth_token || '';
        document.getElementById('central_url').value = cfg.central_url || '';
        document.getElementById('intervalo').value   = cfg.interval_minutes || 5;

        // --- 2. Hipervisor (Proxmox / VMware) ---
        if (cfg.proxmox) {
            document.getElementById('hyper_type').value = cfg.proxmox.type || 'proxmox';
            document.getElementById('px_host').value    = cfg.proxmox.host || '';
            document.getElementById('px_node').value    = cfg.proxmox.node || '';
            document.getElementById('px_user').value    = cfg.proxmox.user || '';
            document.getElementById('px_pass').value    = cfg.proxmox.pass || '';
            toggleHypervisorFields(); // Ajusta la visibilidad según el tipo
        }
        const chkProxmox = document.getElementById('enable_proxmox');
        chkProxmox.checked = !!cfg.enabled_proxmox;
        toggleCard('proxmox_body', chkProxmox);

        // --- 3. Hardware Dell (iDRAC) ---
        if (cfg.idrac) {
            document.getElementById('idrac_ip').value   = cfg.idrac.ip || '';
            document.getElementById('idrac_user').value = cfg.idrac.user || '';
            document.getElementById('idrac_pass').value = cfg.idrac.pass || '';
        }
        const chkIdrac = document.getElementById('enable_idrac');
        chkIdrac.checked = !!cfg.enabled_idrac;
        toggleCard('idrac_body', chkIdrac);

        // --- 4. Equipos Windows (VMs) ---
        const chkVms = document.getElementById('enable_vms');
        chkVms.checked = !!cfg.enabled_vms;
        toggleCard('vms_body', chkVms);

        const vmsContainer = document.getElementById('vms_list');
        vmsContainer.innerHTML = '';
        if (cfg.vms && cfg.vms.length > 0) {
            cfg.vms.forEach(vm => agregarVM(vm));
        }

        // --- 5. Métricas de Negocio (SQL) ---
        if (cfg.sql) {
            document.getElementById('sql_host').value       = cfg.sql.host || '';
            document.getElementById('sql_db').value         = cfg.sql.db || 'ExtensaRadio';
            document.getElementById('sql_user').value       = cfg.sql.user || '';
            document.getElementById('sql_pass').value       = cfg.sql.pass || '';
            document.getElementById('sql_exec_day').value   = cfg.sql.executions_per_day || 3;
            document.getElementById('sql_start_date').value = cfg.sql.historical_start_date || '';
        }
        const chkSql = document.getElementById('enable_sql');
        chkSql.checked = !!cfg.enabled_sql;
        toggleCard('sql_body', chkSql);

        // --- 6. Integraciones (Mirth Connect) --- (NUEVO v4.1)
        const chkMirth = document.getElementById('enable_mirth');
        chkMirth.checked = !!cfg.enabled_mirth;
        toggleCard('mirth_body', chkMirth);

        const mirthContainer = document.getElementById('mirth_list');
        mirthContainer.innerHTML = '';
        if (cfg.mirth_servers && cfg.mirth_servers.length > 0) {
            cfg.mirth_servers.forEach(m => agregarMirth(m));
        }

        // --- 7. Certificados SSL --- (NUEVO v4.2)
        const chkSsl = document.getElementById('enable_ssl');
        chkSsl.checked = !!cfg.enabled_ssl;
        toggleCard('ssl_body', chkSsl);

        const sslContainer = document.getElementById('ssl_list');
        sslContainer.innerHTML = '';
        if (cfg.ssl_urls && cfg.ssl_urls.length > 0) {
            cfg.ssl_urls.forEach(urlObj => agregarSSL(urlObj));
        }

        // --- 8. ElasticSearch: Logs + Autoenrute --- (v4.4)
        if (cfg.elastic) {
            document.getElementById('elastic_host').value          = cfg.elastic.host || '';
            document.getElementById('elastic_port').value          = cfg.elastic.port || 9200;
            document.getElementById('elastic_user').value          = cfg.elastic.user || '';
            document.getElementById('elastic_pass').value          = cfg.elastic.pass || '';
            document.getElementById('elastic_index_pattern').value = cfg.elastic.index_pattern || 'se-es-logging-*';
            document.getElementById('elastic_dicom_index').value   = cfg.elastic.dicom_index || 'ext_dicom_queues';
            document.getElementById('elastic_dicom_max_age').value = cfg.elastic.dicom_max_age_minutes || 15;

            // --- NUEVO v4.5: KPIs de RIS vía Elastic ---
            document.getElementById('elastic_ris_exec_day').value    = cfg.elastic.ris_executions_per_day || 3;
            document.getElementById('elastic_ris_start_date').value  = cfg.elastic.ris_historical_start_date || '';
            document.getElementById('elastic_ris_index_ris').value   = cfg.elastic.ris_index_ris   || 'ext_ris_metrics_hourly';
            document.getElementById('elastic_ris_index_pacs').value  = cfg.elastic.ris_index_pacs  || 'ext_pacs_metrics_hourly';
            document.getElementById('elastic_ris_index_users').value = cfg.elastic.ris_index_users || 'ext_users_metrics_hourly';
        }
        const chkElastic = document.getElementById('enable_elastic');
        chkElastic.checked = !!cfg.enabled_elastic;

        // COMPATIBILIDAD: hasta v4.3 el flag vivía dentro de cfg.sql.
        // Sin este fallback, los agentes ya desplegados aparecerían con el
        // switch apagado y dejarían de reportar en silencio al primer guardado.
        const dicomRoutingActivo =
            (cfg.elastic && typeof cfg.elastic.enabled_dicom_routing === 'boolean')
                ? cfg.elastic.enabled_dicom_routing
                : !!(cfg.sql && cfg.sql.enabled_dicom_routing);

        const chkDicom = document.getElementById('enabled_dicom_routing');
        chkDicom.checked = dicomRoutingActivo;

        const chkRisMetrics = document.getElementById('enabled_ris_metrics');
        chkRisMetrics.checked = !!(cfg.elastic && cfg.elastic.enabled_ris_metrics);

        // toggleCard debe correr DESPUÉS de fijar los sub-switches: si la
        // tarjeta Elastic está apagada, se encarga de bajarlos por coherencia.
        toggleCard('elastic_body', chkElastic);
        toggleDicomRouting(chkDicom);
        toggleRisMetrics(chkRisMetrics);

    } catch (e) {
        console.error("Error crítico al cargar configuración:", e);
    }
}

async function guardarConfiguracion() {
    const btn = document.querySelector('button[onclick="guardarConfiguracion()"]');
    const originalText = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Guardando...';
    btn.disabled = true;

    // --- Recolectar lista de VMs ---
    const vms = [];
    document.querySelectorAll('.vm-card').forEach(card => {
        vms.push({
            nombre:    card.querySelector('.vm-nombre').value.trim(),
            type:      card.querySelector('.vm-type').value,
            ip:        card.querySelector('.vm-ip').value.trim(),
            user:      card.querySelector('.vm-user').value.trim(),
            pass:      card.querySelector('.vm-pass').value,
            servicios: card.querySelector('.vm-servicios').value.trim(),
        });
    });

    // --- Recolectar lista de Mirth Connect --- (NUEVO v4.1)
    const mirth_servers = [];
    document.querySelectorAll('.mirth-card').forEach(card => {
        mirth_servers.push({
            alias: card.querySelector('.mirth-alias').value.trim(),
            url:   card.querySelector('.mirth-url').value.trim(),
            user:  card.querySelector('.mirth-user').value.trim(),
            pass:  card.querySelector('.mirth-pass').value,
        });
    });

    // --- Recolectar lista de SSL --- (NUEVO v4.2)
    const ssl_urls = [];
    document.querySelectorAll('.ssl-card').forEach(card => {
        ssl_urls.push({
            url: card.querySelector('.ssl-url').value.trim()
        });
    });

    // --- Construir objeto de configuración Maestro ---
    const config = {
        hospital_id:      document.getElementById('hosp_id').value.trim(),
        auth_token:       document.getElementById('auth_token').value,
        central_url:      document.getElementById('central_url').value.trim(),
        interval_minutes: parseInt(document.getElementById('intervalo').value) || 5,

        enabled_proxmox: document.getElementById('enable_proxmox').checked,
        proxmox: {
            type: document.getElementById('hyper_type').value,
            host: document.getElementById('px_host').value.trim(),
            node: document.getElementById('px_node').value.trim(),
            user: document.getElementById('px_user').value.trim(),
            pass: document.getElementById('px_pass').value,
        },

        enabled_idrac: document.getElementById('enable_idrac').checked,
        idrac: {
            ip:   document.getElementById('idrac_ip').value.trim(),
            user: document.getElementById('idrac_user').value.trim(),
            pass: document.getElementById('idrac_pass').value,
        },

        enabled_sql: document.getElementById('enable_sql').checked,
        sql: {
            host:                  document.getElementById('sql_host').value.trim(),
            db:                    document.getElementById('sql_db').value.trim(),
            user:                  document.getElementById('sql_user').value.trim(),
            pass:                  document.getElementById('sql_pass').value,
            executions_per_day:    parseInt(document.getElementById('sql_exec_day').value) || 3,
            historical_start_date: document.getElementById('sql_start_date').value,
        },

        enabled_vms: document.getElementById('enable_vms').checked,
        vms: vms,

        enabled_mirth: document.getElementById('enable_mirth').checked,
        mirth_servers: mirth_servers,

        enabled_ssl: document.getElementById('enable_ssl').checked,
        ssl_urls: ssl_urls,

        // --- v4.4: ElasticSearch (Logs + Autoenrute DICOM) ---
        enabled_elastic: document.getElementById('enable_elastic').checked,
        elastic: {
            host:          document.getElementById('elastic_host').value.trim(),
            port:          parseInt(document.getElementById('elastic_port').value) || 9200,
            user:          document.getElementById('elastic_user').value.trim(),
            pass:          document.getElementById('elastic_pass').value,
            index_pattern: document.getElementById('elastic_index_pattern').value.trim() || 'se-es-logging-*',

            // El flag de autoenrute vive acá desde v4.4 (antes estaba en sql).
            enabled_dicom_routing:  document.getElementById('enabled_dicom_routing').checked,
            dicom_index:            document.getElementById('elastic_dicom_index').value.trim() || 'ext_dicom_queues',
            dicom_max_age_minutes:  parseInt(document.getElementById('elastic_dicom_max_age').value) || 15,

            // --- NUEVO v4.5: KPIs de RIS vía Elastic (alternativa a la tarjeta SQL) ---
            enabled_ris_metrics:     document.getElementById('enabled_ris_metrics').checked,
            ris_executions_per_day:  parseInt(document.getElementById('elastic_ris_exec_day').value) || 3,
            ris_historical_start_date: document.getElementById('elastic_ris_start_date').value,
            ris_index_ris:           document.getElementById('elastic_ris_index_ris').value.trim()   || 'ext_ris_metrics_hourly',
            ris_index_pacs:          document.getElementById('elastic_ris_index_pacs').value.trim()  || 'ext_pacs_metrics_hourly',
            ris_index_users:         document.getElementById('elastic_ris_index_users').value.trim() || 'ext_users_metrics_hourly',
        }
    };

    try {
        const res = await eel.guardar_config(config)();
        
        // Simular tiempo de guardado para feedback visual
        setTimeout(() => {
            btn.disabled = false;
            btn.innerHTML = originalText;
            
            if (res.success) {
                // Bloqueamos el botón de monitoreo 30s mientras el servicio reinicia
                bloquearBotonMonitoreo(30); 
                alert("✅ Configuración guardada correctamente.\nEl servicio se está reiniciando para aplicar los cambios.");
            } else {
                alert("❌ Error al guardar: " + res.msg);
            }
        }, 1000);

    } catch (e) {
        btn.disabled = false;
        btn.innerHTML = originalText;
        alert("Error de comunicación con el motor Python: " + e);
    }
}

// ---------------------------------------------------------------------------
// GESTIÓN DE VMs / WS / EQ (dinámico)
// ---------------------------------------------------------------------------
function agregarVM(data = null) {
    const container   = document.getElementById('vms_list');
    const id          = Date.now();
    const nombreVal   = data?.nombre || "";
    const alias       = nombreVal   || "Equipo Target";
    const selVm       = (data?.type === 'vm') ? 'selected' : '';
    const selWs       = (data?.type === 'ws') ? 'selected' : '';
    const selEq       = (data?.type === 'eq') ? 'selected' : '';
    const defaultType = !data ? 'selected' : '';

    const html = `
    <div class="card p-3 mb-3 border bg-light vm-card" id="vm_${id}">
        <div class="d-flex justify-content-between mb-2">
            <h6 class="fw-bold text-primary mb-0"><i class="fas fa-desktop me-2"></i>${alias}</h6>
            <button class="btn btn-sm btn-outline-danger"
                    onclick="document.getElementById('vm_${id}').remove()">
                <i class="fas fa-trash"></i> Quitar
            </button>
        </div>
        <div class="row g-2">
            <div class="col-md-4">
                <label class="form-label text-muted small mb-0 fw-bold">Nombre (Opcional)</label>
                <input type="text" class="form-control vm-nombre border-primary"
                       placeholder="En blanco = Automático" value="${nombreVal}">
            </div>
            <div class="col-md-4">
                <label class="form-label text-muted small mb-0 fw-bold">Tipo</label>
                <select class="form-select vm-type">
                    <option value="vm" ${selVm || defaultType}>Máquina Virtual (VM)</option>
                    <option value="ws" ${selWs}>Workstation Física (WS)</option>
                    <option value="eq" ${selEq}>Equipo Médico (EQ)</option>
                </select>
            </div>
            <div class="col-md-4">
                <label class="form-label text-muted small mb-0 fw-bold">IP / Hostname</label>
                <input type="text" class="form-control vm-ip"
                       placeholder="Ej: 192.168.1.50" value="${data?.ip || ''}">
            </div>
            <div class="col-md-4 mt-2">
                <input type="text" class="form-control vm-user"
                       placeholder="Admin User" value="${data?.user || ''}">
            </div>
            <div class="col-md-4 mt-2">
                <input type="password" class="form-control vm-pass"
                       placeholder="Password" value="${data?.pass || ''}"
                       oncopy="return false" oncut="return false"
                       autocomplete="new-password">
            </div>
            <div class="col-md-4 mt-2">
                <button class="btn btn-warning w-100 text-white shadow-sm" onclick="testVM(this)">
                    <i class="fas fa-bolt me-1"></i> Test WMI
                </button>
            </div>
            <div class="col-md-12 mt-2">
                <label class="form-label text-muted small mb-0 fw-bold">Servicios a monitorear</label>
                <input type="text" class="form-control vm-servicios"
                       placeholder="Ej: MSSQLSERVER, Spooler"
                       value="${data?.servicios || ''}">
            </div>
        </div>
    </div>`;

    container.insertAdjacentHTML('beforeend', html);
}

// ---------------------------------------------------------------------------
// TESTS DE CONEXIÓN
// ---------------------------------------------------------------------------
async function testCentral() {
    const btn = window.event?.target?.closest('button');
    let originalText = "Test Conexión";
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Probando...'; btn.disabled = true; }

    const url = document.getElementById('central_url').value;
    const res = await eel.probar_conexion_central(url)();

    if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
    alert(res.success
        ? `✅ Conexión Exitosa (HTTP ${res.code})`
        : `❌ Fallo: ${res.msg}`);
}

async function testHypervisor() {
    const type = document.getElementById('hyper_type').value;
    const btn  = window.event?.target?.closest('button');
    let originalText = "Test Conexión";
    if (btn) { originalText = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Probando...'; }

    const data = {
        host: document.getElementById('px_host').value,
        node: document.getElementById('px_node').value,
        user: document.getElementById('px_user').value,
        pass: document.getElementById('px_pass').value,
    };

    try {
        const result = (type === 'vmware')
            ? await eel.test_vmware_gui(data)()
            : await eel.test_proxmox_gui(data)();

        if (btn) { btn.disabled = false; btn.innerHTML = originalText; }

        if (result.success) {
            alert(`✅ ÉXITO:\n${result.msg}`);
        } else {
            // Mensaje más descriptivo para VMware
            if (type === 'vmware' && result.msg.includes('pyVmomi')) {
                alert(`❌ Módulo faltante:\n${result.msg}\n\nEjecutar en el entorno del agente:\n  pip install pyVmomi`);
            } else {
                alert(`❌ ERROR:\n${result.msg}`);
            }
        }
    } catch (e) {
        if (btn) { btn.disabled = false; btn.innerHTML = originalText; }
        alert("Error de comunicación con Python: " + e);
    }
}

async function testIdrac() {
    const btn = window.event?.target?.closest('button');
    let originalText = "Test";
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

    const data = {
        ip:   document.getElementById('idrac_ip').value,
        user: document.getElementById('idrac_user').value,
        pass: document.getElementById('idrac_pass').value,
    };

    try {
        const res = await eel.test_idrac_gui(data)();
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ ${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}

async function testVM(btnElement) {
    const card = btnElement.closest('.vm-card');
    const data = {
        ip:   card.querySelector('.vm-ip').value,
        user: card.querySelector('.vm-user').value,
        pass: card.querySelector('.vm-pass').value,
    };

    const originalHtml = btnElement.innerHTML;
    btnElement.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    btnElement.disabled  = true;

    try {
        const res = await eel.test_vm_gui(data)();
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;

        if (res.success) {
            if (res.hostname) {
                card.querySelector('.vm-nombre').value = res.hostname.toUpperCase();
            }
            alert(`✅ ${res.msg}\n\nEl nombre se autocompletó en el formulario.`);
        } else {
            alert(`❌ ${res.msg}`);
            const currentName = card.querySelector('.vm-nombre').value.trim();
            if (!currentName) {
                const manualName = prompt(
                    "⚠️ WMI falló o el equipo está apagado.\n" +
                    "Ingresá el HOSTNAME real del equipo para evitar duplicados cuando esté disponible:"
                );
                if (manualName?.trim()) {
                    card.querySelector('.vm-nombre').value = manualName.trim().toUpperCase();
                }
            }
        }
    } catch (e) {
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;
        alert("Error de comunicación con Python: " + e);
    }
}

function testSql() {
    alert("⚠️ Para probar SQL, guardá la configuración. El agente intentará conectar en su próximo ciclo.");
}

// ---------------------------------------------------------------------------
// RESET HISTORIAL SQL
// ---------------------------------------------------------------------------
async function resetHistorial() {
    if (confirm(
        "⚠️ ¿Estás seguro?\n\n" +
        "Esto borrará la memoria del Agente y obligará a extraer todos los datos " +
        "históricos desde la 'Fecha Inicio' hasta hoy.\n\n" +
        "Puede tardar varias horas (1 bloque cada intervalo configurado)."
    )) {
        const res = await eel.reset_historial_sql()();
        alert(res
            ? "✅ Memoria borrada. Guardá la configuración para iniciar el Backfill."
            : "ℹ️ No había registro previo o ya estaba limpio.");
    }
}

// ---------------------------------------------------------------------------
// CONTROL DEL SERVICIO
// ---------------------------------------------------------------------------
async function checkStatus() {
    try {
        const running = await eel.check_service_status()();
        updateStatusBadge(running);
    } catch (e) {
        // Falla silenciosa: Python puede estar reiniciando
    }
}

function updateStatusBadge(running) {
    const badge = document.getElementById('service_status_badge');
    const btn   = document.getElementById('btn_monitor_toggle');
    isRunning   = running;

    badge.innerHTML = running
        ? '<span class="badge bg-success shadow fs-6"><i class="fas fa-cog fa-spin me-2"></i>EJECUTANDO (2° Plano)</span>'
        : '<span class="badge bg-secondary shadow fs-6">DETENIDO</span>';

    if (!btn.disabled) {
        if (running) {
            btn.className = 'btn btn-danger btn-lg';
            btn.innerHTML = '<i class="fas fa-stop me-2"></i>Detener Monitoreo';
        } else {
            btn.className = 'btn btn-success btn-lg';
            btn.innerHTML = '<i class="fas fa-play me-2"></i>Iniciar Monitoreo';
        }
    }
}

async function toggleMonitoreo() {
    const accion = !isRunning;
    const btn    = document.getElementById('btn_monitor_toggle');

    btn.disabled  = true;
    btn.className = 'btn btn-secondary btn-lg';

    try {
        const res = await eel.toggle_monitoreo(accion)();

        if (res && res.success === false) {
            btn.disabled = false;
            await checkStatus();
            alert("⚠️ Acción Denegada:\n" + res.msg + "\n\nCerrá el programa y abrilo con 'Ejecutar como administrador'.");
            return;
        }
        bloquearBotonMonitoreo(30);
    } catch (e) {
        btn.disabled = false;
        checkStatus();
    }
}

// ---------------------------------------------------------------------------
// LOGS EN VIVO
// ---------------------------------------------------------------------------
function iniciarLogReader() {
    if (logInterval) clearInterval(logInterval);

    logInterval = setInterval(async () => {
        try {
            const res = await eel.leer_log_delta(logPosition)();
            if (res?.content) {
                const box = document.getElementById('log_console');
                if (logPosition === 0 && box.innerText.includes("Esperando")) {
                    box.innerText = "";
                }
                box.innerText += res.content;
                box.scrollTop  = box.scrollHeight;
                logPosition    = res.pos;
            }
        } catch (e) {
            // Silencioso: Python puede estar recargando
        }
    }, 2000);
}

async function limpiarConsola() {
    const res = await eel.limpiar_log()();
    if (res) {
        document.getElementById('log_console').innerText = "--- Log limpiado por el usuario ---";
        logPosition = 0;
    }
}

function agregarMirth(data = null) {
    const container = document.getElementById('mirth_list');
    const id        = Date.now();
    const aliasVal  = data?.alias || "";

    const html = `
    <div class="card p-3 mb-3 border bg-light mirth-card" id="mirth_${id}">
        <div class="d-flex justify-content-between mb-2">
            <h6 class="fw-bold text-success mb-0"><i class="fas fa-server me-2"></i>Mirth: ${aliasVal || "Nuevo"}</h6>
            <button class="btn btn-sm btn-outline-danger" onclick="document.getElementById('mirth_${id}').remove()">
                <i class="fas fa-trash"></i> Quitar
            </button>
        </div>
        <div class="row g-2">
            <div class="col-md-3">
                <label class="form-label text-muted small fw-bold">Alias / Entorno</label>
                <input type="text" class="form-control mirth-alias border-success" placeholder="Ej: Produccion_Principal" value="${aliasVal}">
            </div>
            <div class="col-md-4">
                <label class="form-label text-muted small fw-bold">URL API (HTTPS)</label>
                <input type="text" class="form-control mirth-url" placeholder="https://192.168.x.x:8443" value="${data?.url || ''}">
            </div>
            <div class="col-md-2">
                <label class="form-label text-muted small fw-bold">Usuario</label>
                <input type="text" class="form-control mirth-user" placeholder="admin" value="${data?.user || ''}">
            </div>
            <div class="col-md-3">
                <label class="form-label text-muted small fw-bold">Contraseña</label>
                <div class="input-group">
                    <input type="password" class="form-control mirth-pass" value="${data?.pass || ''}">
                    <button class="btn btn-warning text-white" onclick="testMirth(this)"><i class="fas fa-plug"></i></button>
                </div>
            </div>
        </div>
    </div>`;
    container.insertAdjacentHTML('beforeend', html);
}

async function testMirth(btnElement) {
    const card = btnElement.closest('.mirth-card');
    const data = {
        url:  card.querySelector('.mirth-url').value,
        user: card.querySelector('.mirth-user').value,
        pass: card.querySelector('.mirth-pass').value,
    };
    
    const originalHtml = btnElement.innerHTML;
    btnElement.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    btnElement.disabled  = true;

    try {
        const res = await eel.test_mirth_gui(data)();
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;
        alert(res.success ? `✅ ${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;
        alert("Error de comunicación: " + e);
    }
}

// ---------------------------------------------------------------------------
// GESTION SSL
// ---------------------------------------------------------------------------
function agregarSSL(data = null) {
    const container = document.getElementById('ssl_list');
    const id        = Date.now();
    const urlVal    = data?.url || "";

    const html = `
    <div class="card p-2 mb-2 border bg-light ssl-card" id="ssl_${id}">
        <div class="row g-2 align-items-center">
            <div class="col-md-8">
                <input type="text" class="form-control ssl-url border-primary" 
                       placeholder="https://pacs.hospital.com" value="${urlVal}">
            </div>
            <div class="col-md-4 d-flex justify-content-end gap-2">
                <button class="btn btn-warning text-white btn-sm" onclick="testSSL(this)" title="Probar conexión">
                    <i class="fas fa-plug"></i> Test
                </button>
                <button class="btn btn-outline-danger btn-sm" onclick="document.getElementById('ssl_${id}').remove()">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        </div>
    </div>`;
    container.insertAdjacentHTML('beforeend', html);
}

async function testSSL(btnElement) {
    const card = btnElement.closest('.ssl-card');
    const url  = card.querySelector('.ssl-url').value.trim();
    
    if (!url) {
        alert("⚠️ Ingresá una URL válida primero.");
        return;
    }

    const originalHtml = btnElement.innerHTML;
    btnElement.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    btnElement.disabled  = true;

    try {
        const res = await eel.test_ssl_gui({url: url})();
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;
        alert(res.success ? `${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        btnElement.innerHTML = originalHtml;
        btnElement.disabled  = false;
        alert("Error de comunicación con Python: " + e);
    }
}

// ---------------------------------------------------------------------------
// GESTION ELASTIC (LOGS + AUTOENRUTE)
// ---------------------------------------------------------------------------
function _leerConfigElasticDesdeUI() {
    return {
        host:        document.getElementById('elastic_host').value.trim(),
        port:        parseInt(document.getElementById('elastic_port').value) || 9200,
        user:        document.getElementById('elastic_user').value.trim(),
        pass:        document.getElementById('elastic_pass').value,
        dicom_index: document.getElementById('elastic_dicom_index').value.trim() || 'ext_dicom_queues',
        ris_index_ris:   document.getElementById('elastic_ris_index_ris').value.trim()   || 'ext_ris_metrics_hourly',
        ris_index_pacs:  document.getElementById('elastic_ris_index_pacs').value.trim()  || 'ext_pacs_metrics_hourly',
        ris_index_users: document.getElementById('elastic_ris_index_users').value.trim() || 'ext_users_metrics_hourly',
    };
}

async function testElastic() {
    const btn = window.event?.target?.closest('button');
    let originalText = '<i class="fas fa-plug"></i> Test';
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

    const data = _leerConfigElasticDesdeUI();

    if (!data.host) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("⚠️ Ingresá el host/IP de ElasticSearch primero.");
        return;
    }

    try {
        const res = await eel.test_elastic_gui(data)();
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ ${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}

// Verifica específicamente que el usuario configurado pueda LEER el índice de
// autoenrute. Un usuario válido para los logs puede no tener permiso sobre
// ext_dicom_queues, y ese 403 es difícil de diagnosticar en producción.
async function testDicomIndex() {
    const btn = window.event?.target?.closest('button');
    let originalText = '<i class="fas fa-plug"></i> Test';
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

    const data = _leerConfigElasticDesdeUI();

    if (!data.host) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("⚠️ Ingresá el host/IP de ElasticSearch primero.");
        return;
    }

    try {
        const res = await eel.test_dicom_index_gui(data)();
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ ${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}

// Igual motivo que testDicomIndex: un usuario válido para un índice puede no
// tener permiso sobre otro. Prueba los tres índices de RIS/PACS/usuarios en
// un solo llamado en vez de tener que probarlos uno por uno.
async function testRisMetrics() {
    const btn = window.event?.target?.closest('button');
    let originalText = '<i class="fas fa-plug"></i> Test (los 3 índices)';
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

    const data = _leerConfigElasticDesdeUI();

    if (!data.host) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("⚠️ Ingresá el host/IP de ElasticSearch primero.");
        return;
    }

    try {
        const res = await eel.test_ris_metrics_gui(data)();
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ Todo OK:\n${res.msg}` : `❌ Hay problemas:\n${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}