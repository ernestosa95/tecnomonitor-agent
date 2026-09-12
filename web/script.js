// ---------------------------------------------------------------------------
// VARIABLES GLOBALES
//
// v4.6: monitor_config.json pasa a {instalaciones: [...], interval_minutes,
// config_version}. `config` guarda ese objeto raíz completo en memoria;
// `perfilActivoIndex` cuál hospital está abierto en el detalle (null en
// Home); `modulosActivos` qué mediciones tiene ese hospital (proxmox, idrac,
// vms, sql, mirth, ssl, elastic) mientras se edita, antes de guardar.
// ---------------------------------------------------------------------------
let config            = { instalaciones: [], config_version: 2, interval_minutes: 5 };
let perfilActivoIndex = null;
let modulosActivos    = [];

let logPosition    = 0;
let logInterval    = null;
let statusInterval = null;
let isRunning      = false;

const MODULOS_DISPONIBLES = {
    proxmox: { label: 'Hipervisor Host',               icon: 'fa-server' },
    idrac:   { label: 'Hardware Dell (iDRAC)',          icon: 'fa-microchip' },
    vms:     { label: 'Equipos Windows (VM/WS)',        icon: 'fa-desktop' },
    sql:     { label: 'Métricas de Negocio (SQL)',      icon: 'fa-database' },
    mirth:   { label: 'Integraciones (Mirth Connect)',  icon: 'fa-network-wired' },
    ssl:     { label: 'Certificados SSL (Web)',         icon: 'fa-lock' },
    elastic: { label: 'ElasticSearch (Logs y Autoenrute)', icon: 'fa-search-location' },
};

// ---------------------------------------------------------------------------
// INICIALIZACIÓN
//
// La carga real de datos arranca recién en onSesionIniciada() (llamada
// desde el overlay de login en index.html, tras verificar_clave o tras la
// pantalla de "primera vez"). Antes de eso, la API de Python rechaza todo
// método sensible con {ok:false, error:"no_autenticado"} — ver
// _requiere_sesion en main_gui.py.
// ---------------------------------------------------------------------------
async function onSesionIniciada() {
    try {
        await cargarConfiguracion();
        renderHome();
        iniciarLogReader();
        await checkStatus();
        statusInterval = setInterval(checkStatus, 3000);
    } catch (err) {
        console.error("Error en la inicialización:", err);
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// UI — hipervisor y sub-toggles de ElasticSearch
// ---------------------------------------------------------------------------
function toggleDicomRouting(checkbox) {
    const el = document.getElementById('dicom_routing_fields');
    if (!el) return;
    el.style.opacity       = checkbox.checked ? '1'    : '0.5';
    el.style.pointerEvents = checkbox.checked ? 'auto' : 'none';
}

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
// BLOQUEO VISUAL DEL BOTÓN DESPUÉS DE GUARDAR (guardar reinicia el servicio
// completo — sirve todos los hospitales del mismo proceso)
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

// ---------------------------------------------------------------------------
// CARGA / GUARDADO DE CONFIGURACIÓN (objeto raíz completo)
// ---------------------------------------------------------------------------
async function cargarConfiguracion() {
    try {
        const cfg = await pywebview.api.cargar_config();
        if (!cfg || cfg._error) {
            console.warn("Configuración vacía o con error:", cfg && cfg._error);
            config = { instalaciones: [], config_version: 2, interval_minutes: 5 };
            return;
        }
        config = cfg;
        if (!Array.isArray(config.instalaciones)) config.instalaciones = [];
        if (!config.interval_minutes) config.interval_minutes = 5;
    } catch (e) {
        console.error("Error crítico al cargar configuración:", e);
        config = { instalaciones: [], config_version: 2, interval_minutes: 5 };
    }
}

async function persistirConfig(opts = {}) {
    try {
        const res = await pywebview.api.guardar_config(config);
        if (res && res.success) {
            bloquearBotonMonitoreo(30);
            if (!opts.silencioso) {
                alert("✅ Configuración guardada correctamente.\nEl servicio se está reiniciando para aplicar los cambios.");
            }
            if (res.warning) alert("⚠️ " + res.msg);
        } else {
            alert("❌ Error al guardar: " + (res && res.msg));
        }
        return res;
    } catch (e) {
        alert("Error de comunicación con el motor Python: " + e);
        return { success: false, msg: String(e) };
    }
}

// ---------------------------------------------------------------------------
// HOME — grid de tarjetas de hospital
// ---------------------------------------------------------------------------
function mostrarHome() {
    perfilActivoIndex = null;
    document.getElementById('hospitalDetailView').style.display = 'none';
    document.getElementById('homeView').style.display = 'block';
    document.getElementById('btnVolverHome').style.display = 'none';
    document.getElementById('tituloHeader').textContent = 'TecnoMonitor Agent';
    document.getElementById('subtituloHeader').textContent = 'Configuración Local';
    renderHome();
}

function mostrarDetalle() {
    document.getElementById('homeView').style.display = 'none';
    document.getElementById('hospitalDetailView').style.display = 'block';
    document.getElementById('btnVolverHome').style.display = 'inline-block';
}

function determinarModulosActivos(perfil) {
    const activos = [];
    if (perfil.enabled_proxmox) activos.push('proxmox');
    if (perfil.enabled_idrac)   activos.push('idrac');
    if (perfil.enabled_vms)     activos.push('vms');
    if (perfil.enabled_sql)     activos.push('sql');
    if (perfil.enabled_mirth)   activos.push('mirth');
    if (perfil.enabled_ssl)     activos.push('ssl');
    if (perfil.enabled_elastic) activos.push('elastic');
    return activos;
}

function renderHome() {
    const grid = document.getElementById('homeGrid');
    grid.innerHTML = '';
    document.getElementById('intervalo').value = config.interval_minutes || 5;

    (config.instalaciones || []).forEach((perfil, i) => {
        const activo = perfil.enabled !== false;
        const div = document.createElement('div');
        div.className = 'hospital-card';
        div.innerHTML = `
            <div class="card-top">
                <span class="hospital-id"><i class="fas fa-hospital me-1"></i>${escapeHtml(perfil.hospital_id || '(sin nombre)')}</span>
                <span class="badge ${activo ? 'bg-success' : 'bg-secondary'}">${activo ? 'Activo' : 'Inactivo'}</span>
            </div>
            <small class="text-muted">${determinarModulosActivos(perfil).length} mediciones configuradas</small>
            <div class="d-flex justify-content-between align-items-center mt-2">
                <div class="form-check form-switch m-0"></div>
                <button class="btn btn-sm btn-outline-danger btn-eliminar-hospital" title="Eliminar hospital">
                    <i class="fas fa-trash"></i>
                </button>
            </div>`;

        const switchWrap = div.querySelector('.form-check.form-switch');
        const switchInput = document.createElement('input');
        switchInput.className = 'form-check-input';
        switchInput.type = 'checkbox';
        switchInput.checked = activo;
        switchInput.addEventListener('click', (e) => e.stopPropagation());
        switchInput.addEventListener('change', (e) => toggleHospitalActivo(i, e.target.checked));
        switchWrap.appendChild(switchInput);

        div.querySelector('.btn-eliminar-hospital').addEventListener('click', (e) => {
            e.stopPropagation();
            eliminarHospital(i);
        });

        div.addEventListener('click', () => abrirHospital(i));
        grid.appendChild(div);
    });

    const addCard = document.createElement('div');
    addCard.className = 'add-card';
    addCard.innerHTML = '<i class="fas fa-plus fa-2x mb-2"></i><div>Agregar hospital</div>';
    addCard.addEventListener('click', agregarHospital);
    grid.appendChild(addCard);
}

async function toggleHospitalActivo(i, checked) {
    config.instalaciones[i].enabled = checked;
    await persistirConfig({ silencioso: true });
    renderHome();
}

async function eliminarHospital(i) {
    const perfil = config.instalaciones[i];
    if (!confirm(`¿Eliminar el hospital "${perfil.hospital_id || '(sin nombre)'}"?\n\nEsta acción no se puede deshacer.`)) return;
    config.instalaciones.splice(i, 1);
    await persistirConfig({ silencioso: true });
    if (perfilActivoIndex === i) {
        mostrarHome();
    } else {
        renderHome();
    }
}

async function eliminarHospitalActivo() {
    if (perfilActivoIndex === null) return;
    await eliminarHospital(perfilActivoIndex);
}

function agregarHospital() {
    const id = prompt("ID del nuevo hospital (ej: H42):");
    if (!id || !id.trim()) return;

    const nuevo = {
        hospital_id: id.trim(),
        auth_token: '',
        central_url: (config.instalaciones[0] && config.instalaciones[0].central_url) || '',
        enabled: true,
    };
    config.instalaciones.push(nuevo);
    persistirConfig({ silencioso: true }).then(() => {
        abrirHospital(config.instalaciones.length - 1);
    });
}

// ---------------------------------------------------------------------------
// DETALLE DE HOSPITAL — config general + tarjetas de módulo
// ---------------------------------------------------------------------------
function poblarFormularioModulos(perfil) {
    // Hipervisor
    const px = perfil.proxmox || {};
    document.getElementById('hyper_type').value = px.type || 'proxmox';
    document.getElementById('px_host').value    = px.host || '';
    document.getElementById('px_node').value    = px.node || '';
    document.getElementById('px_user').value    = px.user || '';
    document.getElementById('px_pass').value    = px.pass || '';
    toggleHypervisorFields();

    // iDRAC
    const idrac = perfil.idrac || {};
    document.getElementById('idrac_ip').value   = idrac.ip || '';
    document.getElementById('idrac_user').value = idrac.user || '';
    document.getElementById('idrac_pass').value = idrac.pass || '';

    // Equipos Windows (VMs)
    const vmsContainer = document.getElementById('vms_list');
    vmsContainer.innerHTML = '';
    (perfil.vms || []).forEach(vm => agregarVM(vm));

    // SQL
    const sql = perfil.sql || {};
    document.getElementById('sql_host').value       = sql.host || '';
    document.getElementById('sql_db').value         = sql.db || 'ExtensaRadio';
    document.getElementById('sql_user').value       = sql.user || '';
    document.getElementById('sql_pass').value       = sql.pass || '';
    document.getElementById('sql_exec_day').value   = sql.executions_per_day || 3;
    document.getElementById('sql_start_date').value = sql.historical_start_date || '';

    // Mirth
    const mirthContainer = document.getElementById('mirth_list');
    mirthContainer.innerHTML = '';
    (perfil.mirth_servers || []).forEach(m => agregarMirth(m));

    // SSL
    const sslContainer = document.getElementById('ssl_list');
    sslContainer.innerHTML = '';
    (perfil.ssl_urls || []).forEach(u => agregarSSL(u));

    // ElasticSearch (logs + autoenrute + KPIs de RIS)
    const el = perfil.elastic || {};
    document.getElementById('elastic_host').value          = el.host || '';
    document.getElementById('elastic_port').value          = el.port || 9200;
    document.getElementById('elastic_user').value          = el.user || '';
    document.getElementById('elastic_pass').value          = el.pass || '';
    document.getElementById('elastic_index_pattern').value = el.index_pattern || 'se-es-logging-*';
    document.getElementById('elastic_use_https').checked   = !!el.use_https;
    document.getElementById('elastic_dicom_index').value   = el.dicom_index || 'ext_dicom_queues';
    document.getElementById('elastic_dicom_max_age').value = el.dicom_max_age_minutes || 15;

    document.getElementById('elastic_ris_exec_day').value    = el.ris_executions_per_day || 3;
    document.getElementById('elastic_ris_start_date').value  = el.ris_historical_start_date || '';
    document.getElementById('elastic_ris_index_ris').value   = el.ris_index_ris   || 'ext_ris_metrics_hourly';
    document.getElementById('elastic_ris_index_pacs').value  = el.ris_index_pacs  || 'ext_pacs_metrics_hourly';
    document.getElementById('elastic_ris_index_users').value = el.ris_index_users || 'ext_users_metrics_hourly';

    // COMPATIBILIDAD: hasta v4.3 el flag de autoenrute vivía dentro de sql.
    const dicomRoutingActivo =
        typeof el.enabled_dicom_routing === 'boolean'
            ? el.enabled_dicom_routing
            : !!(sql && sql.enabled_dicom_routing);

    const chkDicom = document.getElementById('enabled_dicom_routing');
    chkDicom.checked = dicomRoutingActivo;
    toggleDicomRouting(chkDicom);

    const chkRisMetrics = document.getElementById('enabled_ris_metrics');
    chkRisMetrics.checked = !!el.enabled_ris_metrics;
    toggleRisMetrics(chkRisMetrics);
}

function abrirHospital(index) {
    perfilActivoIndex = index;
    const perfil = config.instalaciones[index];

    document.getElementById('hosp_id').value     = perfil.hospital_id || '';
    document.getElementById('auth_token').value  = perfil.auth_token || '';
    document.getElementById('central_url').value = perfil.central_url || '';

    poblarFormularioModulos(perfil);
    modulosActivos = determinarModulosActivos(perfil);

    document.getElementById('tituloHeader').textContent = perfil.hospital_id || '(sin nombre)';
    document.getElementById('subtituloHeader').textContent =
        perfil.enabled === false ? 'Hospital desactivado' : 'Configuración del hospital';

    renderModulosActivos();
    mostrarDetalle();
}

async function guardarPerfilActivo() {
    if (perfilActivoIndex === null) return;

    const btn = document.querySelector('button[onclick="guardarPerfilActivo()"]');
    const originalText = btn ? btn.innerHTML : null;
    if (btn) { btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Guardando...'; btn.disabled = true; }

    // --- Recolectar lista de VMs ---
    const vms = [];
    document.querySelectorAll('.vm-card').forEach(card => {
        vms.push({
            nombre:    card.querySelector('.vm-nombre').value.trim(),
            type:      card.querySelector('.vm-type').value,
            os:        card.querySelector('.vm-os').value,
            ip:        card.querySelector('.vm-ip').value.trim(),
            user:      card.querySelector('.vm-user').value.trim(),
            pass:      card.querySelector('.vm-pass').value,
            servicios: card.querySelector('.vm-servicios').value.trim(),
        });
    });

    // --- Recolectar lista de Mirth Connect ---
    const mirth_servers = [];
    document.querySelectorAll('.mirth-card').forEach(card => {
        mirth_servers.push({
            alias: card.querySelector('.mirth-alias').value.trim(),
            url:   card.querySelector('.mirth-url').value.trim(),
            user:  card.querySelector('.mirth-user').value.trim(),
            pass:  card.querySelector('.mirth-pass').value,
        });
    });

    // --- Recolectar lista de SSL ---
    const ssl_urls = [];
    document.querySelectorAll('.ssl-card').forEach(card => {
        ssl_urls.push({ url: card.querySelector('.ssl-url').value.trim() });
    });

    const perfilPrevio = config.instalaciones[perfilActivoIndex] || {};

    const perfil = {
        hospital_id:  document.getElementById('hosp_id').value.trim(),
        auth_token:   document.getElementById('auth_token').value,
        central_url:  document.getElementById('central_url').value.trim(),
        enabled:      perfilPrevio.enabled !== false,

        enabled_proxmox: modulosActivos.includes('proxmox'),
        proxmox: {
            type: document.getElementById('hyper_type').value,
            host: document.getElementById('px_host').value.trim(),
            node: document.getElementById('px_node').value.trim(),
            user: document.getElementById('px_user').value.trim(),
            pass: document.getElementById('px_pass').value,
        },

        enabled_idrac: modulosActivos.includes('idrac'),
        idrac: {
            ip:   document.getElementById('idrac_ip').value.trim(),
            user: document.getElementById('idrac_user').value.trim(),
            pass: document.getElementById('idrac_pass').value,
        },

        enabled_sql: modulosActivos.includes('sql'),
        sql: {
            host:                  document.getElementById('sql_host').value.trim(),
            db:                    document.getElementById('sql_db').value.trim(),
            user:                  document.getElementById('sql_user').value.trim(),
            pass:                  document.getElementById('sql_pass').value,
            executions_per_day:    parseInt(document.getElementById('sql_exec_day').value) || 3,
            historical_start_date: document.getElementById('sql_start_date').value,
        },

        enabled_vms: modulosActivos.includes('vms'),
        vms: vms,

        enabled_mirth: modulosActivos.includes('mirth'),
        mirth_servers: mirth_servers,

        enabled_ssl: modulosActivos.includes('ssl'),
        ssl_urls: ssl_urls,

        enabled_elastic: modulosActivos.includes('elastic'),
        elastic: {
            host:          document.getElementById('elastic_host').value.trim(),
            port:          parseInt(document.getElementById('elastic_port').value) || 9200,
            user:          document.getElementById('elastic_user').value.trim(),
            pass:          document.getElementById('elastic_pass').value,
            index_pattern: document.getElementById('elastic_index_pattern').value.trim() || 'se-es-logging-*',
            use_https:     document.getElementById('elastic_use_https').checked,

            enabled_dicom_routing:  document.getElementById('enabled_dicom_routing').checked,
            dicom_index:            document.getElementById('elastic_dicom_index').value.trim() || 'ext_dicom_queues',
            dicom_max_age_minutes:  parseInt(document.getElementById('elastic_dicom_max_age').value) || 15,

            enabled_ris_metrics:      document.getElementById('enabled_ris_metrics').checked,
            ris_executions_per_day:   parseInt(document.getElementById('elastic_ris_exec_day').value) || 3,
            ris_historical_start_date: document.getElementById('elastic_ris_start_date').value,
            ris_index_ris:            document.getElementById('elastic_ris_index_ris').value.trim()   || 'ext_ris_metrics_hourly',
            ris_index_pacs:           document.getElementById('elastic_ris_index_pacs').value.trim()  || 'ext_pacs_metrics_hourly',
            ris_index_users:          document.getElementById('elastic_ris_index_users').value.trim() || 'ext_users_metrics_hourly',
        },
    };

    config.instalaciones[perfilActivoIndex] = perfil;

    await persistirConfig();

    if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
}

async function guardarIntervaloGlobal() {
    config.interval_minutes = parseInt(document.getElementById('intervalo').value) || 5;
    await persistirConfig();
}

async function cambiarCodigoAcceso() {
    if (!confirm(
        "¿Generar un código de acceso nuevo para esta GUI?\n\n" +
        "El código actual dejará de funcionar de inmediato."
    )) {
        return;
    }
    try {
        const res = await pywebview.api.cambiar_codigo_gui();
        if (res && res.ok) {
            alert(
                "🔑 Nuevo código de acceso:\n\n" + res.codigo + "\n\n" +
                "Guardalo ahora en un lugar seguro — no se va a volver a mostrar."
            );
        } else {
            alert("❌ No se pudo cambiar el código.");
        }
    } catch (e) {
        alert("Error de comunicación con Python: " + e);
    }
}

// ---------------------------------------------------------------------------
// TARJETAS DE MÓDULO (mediciones activas del hospital abierto)
// ---------------------------------------------------------------------------
function renderModulosActivos() {
    const grid = document.getElementById('modulosGrid');
    grid.innerHTML = '';

    modulosActivos.forEach(key => {
        const info = MODULOS_DISPONIBLES[key];
        if (!info) return;
        const div = document.createElement('div');
        div.className = 'modulo-card';
        div.innerHTML = `<i class="fas ${info.icon} fa-lg text-primary mb-1"></i><span class="modulo-titulo">${info.label}</span>`;
        div.addEventListener('click', () => abrirModalModulo(key));
        grid.appendChild(div);
    });

    const addCard = document.createElement('div');
    addCard.className = 'add-card';
    addCard.innerHTML = '<i class="fas fa-plus fa-2x mb-2"></i><div>Agregar medición</div>';
    addCard.addEventListener('click', abrirPickerModulos);
    grid.appendChild(addCard);
}

function abrirModalModulo(key) {
    const modalEl = document.getElementById('modal_' + key);
    if (!modalEl) return;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

function abrirPickerModulos() {
    const lista = document.getElementById('pickerModulosLista');
    lista.innerHTML = '';
    const disponibles = Object.keys(MODULOS_DISPONIBLES).filter(k => !modulosActivos.includes(k));

    if (disponibles.length === 0) {
        lista.innerHTML = '<div class="list-group-item text-muted">Ya agregaste todas las mediciones disponibles.</div>';
    } else {
        disponibles.forEach(key => {
            const info = MODULOS_DISPONIBLES[key];
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'list-group-item list-group-item-action picker-item';
            item.innerHTML = `<i class="fas ${info.icon} me-2 text-primary"></i>${info.label}`;
            item.addEventListener('click', () => agregarModulo(key));
            lista.appendChild(item);
        });
    }

    bootstrap.Modal.getOrCreateInstance(document.getElementById('modalAgregarMedicion')).show();
}

function agregarModulo(key) {
    if (!modulosActivos.includes(key)) modulosActivos.push(key);
    bootstrap.Modal.getInstance(document.getElementById('modalAgregarMedicion'))?.hide();
    renderModulosActivos();
    // Abrimos directo el formulario recién agregado para completarlo.
    setTimeout(() => abrirModalModulo(key), 300);
}

function quitarModulo(key) {
    const info = MODULOS_DISPONIBLES[key];
    if (!confirm(`¿Quitar la medición "${info.label}"?\n\nLos datos ingresados en este formulario se perderán si no guardaste antes.`)) return;
    modulosActivos = modulosActivos.filter(k => k !== key);
    bootstrap.Modal.getInstance(document.getElementById('modal_' + key))?.hide();
    renderModulosActivos();
}

// ---------------------------------------------------------------------------
// GESTIÓN DE VMs / WS / EQ (dinámico)
// ---------------------------------------------------------------------------
const PLACEHOLDER_SERVICIOS = {
    windows: "Ej: MSSQLSERVER, Spooler",
    linux:   "Ej: postgresql, logstash",
};

function agregarVM(data = null) {
    const container   = document.getElementById('vms_list');
    const id          = Date.now();
    const nombreVal   = data?.nombre || "";
    const alias       = nombreVal   || "Equipo Target";
    const selVm       = (data?.type === 'vm') ? 'selected' : '';
    const selWs       = (data?.type === 'ws') ? 'selected' : '';
    const selEq       = (data?.type === 'eq') ? 'selected' : '';
    const defaultType = !data ? 'selected' : '';
    // Sin "os" en los datos guardados (configs de antes de v4.6) = Windows,
    // mismo comportamiento de siempre — ver docs/PLAN_MEJORAS_V4.5.md §9.2.
    const os          = data?.os === 'linux' ? 'linux' : 'windows';
    const selWin      = os === 'windows' ? 'selected' : '';
    const selLinux    = os === 'linux'   ? 'selected' : '';

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
                <label class="form-label text-muted small mb-0 fw-bold">Sistema Operativo</label>
                <select class="form-select vm-os" onchange="actualizarPlaceholderServicios(this)">
                    <option value="windows" ${selWin}>Windows (WMI)</option>
                    <option value="linux" ${selLinux}>Linux (SSH)</option>
                </select>
            </div>
            <div class="col-md-4 mt-2">
                <label class="form-label text-muted small mb-0 fw-bold">IP / Hostname</label>
                <input type="text" class="form-control vm-ip"
                       placeholder="Ej: 192.168.1.50" value="${data?.ip || ''}">
            </div>
            <div class="col-md-4 mt-2">
                <input type="text" class="form-control vm-user"
                       placeholder="Usuario" value="${data?.user || ''}">
            </div>
            <div class="col-md-4 mt-2">
                <input type="password" class="form-control vm-pass"
                       placeholder="Password" value="${data?.pass || ''}"
                       oncopy="return false" oncut="return false"
                       autocomplete="new-password">
            </div>
            <div class="col-md-4 mt-2">
                <button class="btn btn-warning w-100 text-white shadow-sm" onclick="testVM(this)">
                    <i class="fas fa-bolt me-1"></i> Test conexión
                </button>
            </div>
            <div class="col-md-12 mt-2">
                <label class="form-label text-muted small mb-0 fw-bold">Servicios a monitorear</label>
                <input type="text" class="form-control vm-servicios"
                       placeholder="${PLACEHOLDER_SERVICIOS[os]}"
                       value="${data?.servicios || ''}">
            </div>
        </div>
    </div>`;

    container.insertAdjacentHTML('beforeend', html);
}

function actualizarPlaceholderServicios(selectOs) {
    const card = selectOs.closest('.vm-card');
    card.querySelector('.vm-servicios').placeholder = PLACEHOLDER_SERVICIOS[selectOs.value];
}

// ---------------------------------------------------------------------------
// TESTS DE CONEXIÓN
// ---------------------------------------------------------------------------
async function testCentral() {
    const btn = window.event?.target?.closest('button');
    let originalText = "Test Conexión";
    if (btn) { originalText = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Probando...'; btn.disabled = true; }

    const url = document.getElementById('central_url').value;
    const res = await pywebview.api.probar_conexion_central(url);

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
            ? await pywebview.api.test_vmware_gui(data)
            : await pywebview.api.test_proxmox_gui(data);

        if (btn) { btn.disabled = false; btn.innerHTML = originalText; }

        if (result.success) {
            alert(`✅ ÉXITO:\n${result.msg}`);
        } else {
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
        const res = await pywebview.api.test_idrac_gui(data);
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ ${res.msg}` : `❌ ${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}

async function testVM(btnElement) {
    const card = btnElement.closest('.vm-card');
    const os   = card.querySelector('.vm-os').value;
    const data = {
        ip:   card.querySelector('.vm-ip').value,
        user: card.querySelector('.vm-user').value,
        pass: card.querySelector('.vm-pass').value,
    };

    const originalHtml = btnElement.innerHTML;
    btnElement.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    btnElement.disabled  = true;

    try {
        const res = (os === 'linux')
            ? await pywebview.api.test_vm_ssh_gui(data)
            : await pywebview.api.test_vm_gui(data);
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
                    `⚠️ ${os === 'linux' ? 'SSH falló' : 'WMI falló'} o el equipo está apagado.\n` +
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
// RESET HISTORIAL SQL — v4.6: el checkpoint es por hospital_id
// ---------------------------------------------------------------------------
async function resetHistorial() {
    if (perfilActivoIndex === null) return;
    const hospitalId = document.getElementById('hosp_id').value.trim();

    if (confirm(
        "⚠️ ¿Estás seguro?\n\n" +
        "Esto borrará la memoria del Agente para este hospital y obligará a extraer " +
        "todos los datos históricos desde la 'Fecha Inicio' hasta hoy.\n\n" +
        "Puede tardar varias horas (1 bloque cada intervalo configurado)."
    )) {
        const res = await pywebview.api.reset_historial_sql(hospitalId);
        alert(res
            ? "✅ Memoria borrada. Guardá la configuración para iniciar el Backfill."
            : "ℹ️ No había registro previo o ya estaba limpio.");
    }
}

// ---------------------------------------------------------------------------
// CONTROL DEL SERVICIO (global — un único proceso sirve todos los hospitales)
// ---------------------------------------------------------------------------
async function checkStatus() {
    try {
        const running = await pywebview.api.check_service_status();
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
        const res = await pywebview.api.toggle_monitoreo(accion);

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
// LOGS EN VIVO (global — todos los hospitales comparten activity.log)
// ---------------------------------------------------------------------------
function iniciarLogReader() {
    if (logInterval) clearInterval(logInterval);

    logInterval = setInterval(async () => {
        try {
            const res = await pywebview.api.leer_log_delta(logPosition);
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
    const res = await pywebview.api.limpiar_log();
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
        const res = await pywebview.api.test_mirth_gui(data);
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
        const res = await pywebview.api.test_ssl_gui({ url: url });
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
// GESTION ELASTIC (LOGS + AUTOENRUTE + KPIs DE RIS)
// ---------------------------------------------------------------------------
function _leerConfigElasticDesdeUI() {
    return {
        host:        document.getElementById('elastic_host').value.trim(),
        port:        parseInt(document.getElementById('elastic_port').value) || 9200,
        user:        document.getElementById('elastic_user').value.trim(),
        pass:        document.getElementById('elastic_pass').value,
        use_https:   document.getElementById('elastic_use_https').checked,
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
        const res = await pywebview.api.test_elastic_gui(data);
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
        const res = await pywebview.api.test_dicom_index_gui(data);
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
        const res = await pywebview.api.test_ris_metrics_gui(data);
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert(res.success ? `✅ Todo OK:\n${res.msg}` : `❌ Hay problemas:\n${res.msg}`);
    } catch (e) {
        if (btn) { btn.innerHTML = originalText; btn.disabled = false; }
        alert("Error de comunicación con Python: " + e);
    }
}
