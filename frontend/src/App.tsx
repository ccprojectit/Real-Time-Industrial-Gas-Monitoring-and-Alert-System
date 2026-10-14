import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, Bell, Check, ChevronDown, Clock3, Flame, Gauge, Radio, RotateCcw, ShieldCheck, Siren, TriangleAlert, Wind } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { calculateDashboardSummary } from './dashboardSummary.mjs'

const API = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
type Zone = { id:number; name:string; gas_name:string; formula:string; unit:string; warning_threshold:number; critical_threshold:number; current_reading:number|null; previous_reading:number|null; status:string; last_updated:string|null; active_incident_id:string|null }
type Reading = { id:number; zone_id:number; zone_name:string; gas_name:string; value:number; unit:string; measured_at:string }
type Incident = { incident_id:string; zone_id:number; zone_name:string; gas_name:string; formula:string; severity:string; reading:number; unit:string; threshold:number; display_threshold:number; escalation_threshold:number|null; critical_at:string|null; warning_threshold_at_creation:number; critical_threshold_at_creation:number; created_at:string; state:string; acknowledged_at:string|null; acknowledged_by:string|null; resolved_at:string|null; resolved_by:string|null; notification_mode:string|null; notification_status:string|null }
type Notification = { id:string; event_severity:string; mode:string; status:string; message:string; provider_message_id:string|null; created_at:string; zone_name:string; gas_name:string; reading:number; unit:string }
type State = { zones:Zone[]; readings:Reading[]; incidents:Incident[]; notifications:Notification[]; simulator:boolean; authenticated:boolean; authRequired:boolean; demoResetAllowed:boolean; health:string }

async function api<T>(path:string, options:RequestInit = {}):Promise<T> {
  const headers=new Headers(options.headers)
  if(options.body!=null&&!headers.has('Content-Type'))headers.set('Content-Type','application/json')
  const res = await fetch(`${API}${path}`, { ...options, credentials:'include', headers })
  if (!res.ok) { const body = await res.json().catch(()=>({})); throw new Error(body.detail || `Request failed (${res.status})`) }
  return res.json()
}
const dateTime = (s:string|null|undefined) => s ? new Date(s).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—'
const shortTime = (s:string) => new Date(s).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'})

export default function App() {
  const [state,setState]=useState<State>({zones:[],readings:[],incidents:[],notifications:[],simulator:false,authenticated:false,authRequired:true,demoResetAllowed:false,health:'connecting'})
  const [chartZone,setChartZone]=useState<number>(1), [criticalZone,setCriticalZone]=useState<number>(1)
  const [busy,setBusy]=useState(''), [error,setError]=useState(''), [success,setSuccess]=useState('')
  const [now,setNow]=useState(new Date())
  const refreshSequence=useRef(0)
  const historySequence=useRef(0)
  const wsConnected=useRef(false)
  const zonesRef=useRef<Zone[]>([])
  zonesRef.current=state.zones

  const refresh=useCallback(async()=>{
    const sequence=++refreshSequence.current
    try {
      const [s,r,i,n,h,a]=await Promise.all([
        api<{simulator_running:boolean;zones:Zone[]}>('/api/state'),api<Reading[]>('/api/readings?limit=1000'),api<Incident[]>('/api/incidents?limit=100'),api<Notification[]>('/api/notifications?limit=100'),api<{status:string}>('/api/health'),api<{authenticated:boolean;auth_required:boolean;demo_reset_allowed:boolean}>('/api/auth/session')
      ])
      if(sequence!==refreshSequence.current)return false
      setState({zones:s.zones,readings:r,incidents:i,notifications:n,simulator:s.simulator_running,authenticated:a.authenticated,authRequired:a.auth_required,demoResetAllowed:a.demo_reset_allowed,health:h.status});setError('');return true
    } catch(e) { if(sequence!==refreshSequence.current)return false;setState(x=>({...x,health:'disconnected'})); setError(e instanceof Error?e.message:'Unable to connect to the backend');return false }
  },[])
  const refreshHistory=useCallback(async()=>{
    const sequence=++historySequence.current
    try {
      const [incidents,notifications]=await Promise.all([api<Incident[]>('/api/incidents?limit=100'),api<Notification[]>('/api/notifications?limit=100')])
      if(sequence!==historySequence.current)return
      setState(s=>({...s,incidents,notifications}))
    } catch { /* The next WebSocket event or fallback poll can retry. */ }
  },[])
  useEffect(()=>{ void refresh(); const timer=setInterval(()=>{setNow(new Date());if(!wsConnected.current)void refresh()},2000); return()=>clearInterval(timer) },[refresh])
  useEffect(()=>{
    if(state.authRequired&&!state.authenticated)return
    if (!('WebSocket' in window)) return
    let socket:WebSocket|undefined, retry:number
    const connect=()=>{const url=new URL('/api/ws',API);url.protocol=url.protocol==='https:'?'wss:':'ws:';socket=new WebSocket(url);socket.onopen=()=>{wsConnected.current=true;void refresh()};socket.onmessage=event=>{try{const message=JSON.parse(event.data) as {type:string;zones?:Zone[];simulator_running?:boolean};if(message.type!=='state'||!message.zones)return
      const prior=zonesRef.current
      const changed=message.zones.some(z=>{const old=prior.find(p=>p.id===z.id);return old&&(old.status!==z.status||old.active_incident_id!==z.active_incident_id)})
      setState(s=>{let readings=s.readings;for(const zone of message.zones!){if(zone.current_reading==null||!zone.last_updated)continue;const exists=readings.some(r=>r.zone_id===zone.id&&r.measured_at===zone.last_updated);if(!exists)readings=[...readings,{id:Date.parse(zone.last_updated),zone_id:zone.id,zone_name:zone.name,gas_name:zone.gas_name,value:Number(zone.current_reading),unit:zone.unit,measured_at:zone.last_updated}]}return {...s,zones:message.zones!,readings:readings.slice(-1000),simulator:message.simulator_running??s.simulator,health:'connected'}})
      setError('');if(changed)void refreshHistory()
    }catch{/* Ignore malformed frames and keep the fallback poll available. */}};socket.onclose=()=>{wsConnected.current=false;retry=window.setTimeout(connect,3000)};socket.onerror=()=>socket?.close()}
    connect();return()=>{wsConnected.current=false;clearTimeout(retry);socket?.close()}
  },[refresh,refreshHistory,state.authRequired,state.authenticated])
  const canOperate=!state.authRequired||state.authenticated
  const action=async(key:string,path:string,body?:unknown)=>{
    setBusy(key);setError('');setSuccess('')
    try {
      const result=await api<Record<string,any>>(path,{method:'POST',body:body?JSON.stringify(body):undefined})
      if(path==='/api/demo/reset'){
        setState(s=>({...s,readings:[],incidents:[],notifications:[],simulator:false,zones:s.zones.map(z=>({...z,current_reading:null,previous_reading:null,last_updated:null,active_incident_id:null,status:'NORMAL'}))}))
        const refreshed=await refresh()
        setSuccess(refreshed?(result.message||'All simulation data reset successfully.'):'Data reset succeeded, but the dashboard could not refresh. Reload the page.')
      } else if(path.includes('/api/incidents/')){
        const id=path.split('/')[3]
        setState(s=>({...s,incidents:s.incidents.map(i=>i.incident_id===id?{...i,...result}:i)}))
        setSuccess(path.endsWith('/acknowledge')?'Incident acknowledged.':'Incident resolved.')
      } else if(path==='/api/simulation/start'||path==='/api/simulation/stop'){
        setState(s=>({...s,simulator:!!result.simulator_running}))
        setSuccess(result.simulator_running?'Simulation started.':'Simulation stopped.')
      } else if(path==='/api/simulation/critical'){
        const refreshed=await refresh()
        setSuccess(refreshed?'Critical reading and incident recorded.':'Critical event recorded; dashboard refresh is pending.')
      }
    } catch(e) {
      const message=e instanceof Error?e.message:'Action failed'
      if(message.toLowerCase().includes('authentication'))setState(s=>({...s,authenticated:false,authRequired:true}))
      await refresh()
      setError(message)
    } finally {setBusy('')}
  }
  const requestAction=(key:string,path:string,body?:unknown)=>{
    if(!canOperate){setError('This dashboard is read-only without an authorized operator session.');return}
    void action(key,path,body)
  }
  const confirmDemoReset=()=>{if(window.confirm('Reset all simulation data? This stops the simulator and permanently deletes saved readings, incidents, and notifications. The four zones, thresholds, application settings, and audit records will be preserved.'))requestAction('reset','/api/demo/reset')}
  const selectedZone=state.zones.find(z=>z.id===chartZone)
  const chart=useMemo(()=>state.readings.filter(r=>r.zone_id===chartZone).slice(-80).map(r=>({...r,time:shortTime(r.measured_at)})),[state.readings,chartZone])
  const summary=useMemo(()=>calculateDashboardSummary(state.zones,state.incidents),[state.zones,state.incidents])
  return <div className="app-shell">
    <header className="topbar"><div className="brand"><div className="brand-mark"><Activity size={21}/></div><div><h1>Real-Time Industrial Gas Monitoring and Alert System</h1><p>Industrial safety monitoring console</p></div></div><div className="top-actions"><span className={`connection ${state.health==='connected'?'online':''}`}><span className="dot"/>{state.health==='connected'?'System online':state.health==='degraded'?'Database unavailable':'Connecting'}</span><span className="clock"><Clock3 size={15}/>{now.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}</span></div></header>
    <main className="page">
      <section className="welcome-row"><div><h2>Zone overview</h2><p className="muted">Live gas concentrations and system activity across four monitored zones.</p></div><div className="time-stamp"><span className="pulse"/>LIVE <span className="divider">·</span> Updated {now.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'})}</div></section>
      {error&&<div className="alert error"><TriangleAlert size={17}/><span>{error}</span><button onClick={()=>setError('')} aria-label="Dismiss">×</button></div>}
      {success&&<div className="alert success"><ShieldCheck size={17}/><span>{success}</span><button onClick={()=>setSuccess('')} aria-label="Dismiss">×</button></div>}
      <section className="summary-strip"><div className="summary-item"><span className="summary-icon slate"><Radio size={17}/></span><div><strong>{state.zones.length||'—'}</strong><span>Zones monitored</span></div></div><div className="summary-item"><span className="summary-icon green"><ShieldCheck size={17}/></span><div><strong>{summary.normal}</strong><span>Normal zones</span></div></div><div className="summary-item"><span className="summary-icon amber"><AlertTriangle size={17}/></span><div><strong>{summary.warning}</strong><span>Warning zones</span></div></div><div className="summary-item"><span className="summary-icon red"><Siren size={17}/></span><div><strong>{summary.critical}</strong><span>Critical zones</span></div></div><div className="summary-item"><span className="summary-icon blue"><Bell size={17}/></span><div><strong>{summary.openIncidents}</strong><span>Open incidents</span></div></div></section>
      <section className="section-heading"><div><h3>Monitoring zones</h3></div></section>
      <section className="zone-grid">{state.zones.length===0?<div className="empty-panel">{state.health==='disconnected'?'Waiting for backend connection. Start the API and configure the database.':'Loading zone configuration…'}</div>:state.zones.map(zone=><ZoneCard key={zone.id} zone={zone}/>)}</section>
      <section className="control-panel"><div className="control-copy"><div className="control-icon"><Gauge size={18}/></div><div><h3>Simulation controls</h3><p className="muted">Start or stop readings across all zones, or trigger a critical event for one zone.</p></div></div><div className="control-actions"><button className="btn btn-primary" disabled={!canOperate||!!busy||state.simulator} onClick={()=>requestAction('start','/api/simulation/start')}><Activity size={16}/>{busy==='start'?'Starting…':'Start simulation'}</button><button className="btn btn-outline" disabled={!canOperate||!!busy||!state.simulator} onClick={()=>requestAction('stop','/api/simulation/stop')}><span className="stop-square"/>{busy==='stop'?'Stopping…':'Stop simulation'}</button><div className="critical-action"><select aria-label="Critical simulation zone" value={criticalZone} onChange={e=>setCriticalZone(Number(e.target.value))}>{state.zones.map(z=><option key={z.id} value={z.id}>{z.name} · {z.formula}</option>)}</select><button className="btn btn-danger" disabled={!canOperate||!!busy||!state.zones.length} onClick={()=>requestAction('critical','/api/simulation/critical',{zone_id:criticalZone})}><Siren size={16}/>{busy==='critical'?'Triggering…':'Critical simulation'}</button></div><button className="btn btn-light reset-button" disabled={!canOperate||!!busy||!state.zones.length} onClick={confirmDemoReset}><RotateCcw size={15}/>{busy==='reset'?'Resetting…':'Reset All Data'}</button></div>{!canOperate&&<p className="access-note">Live monitoring is available. Operator controls require an authorized session.</p>}</section>
      <section className="content-grid"><div className="panel chart-panel"><div className="panel-head"><div><h3>Reading history</h3><p className="muted">Recent concentration readings for the selected zone</p></div><label className="select-wrap"><select value={chartZone} onChange={e=>setChartZone(Number(e.target.value))}>{state.zones.map(z=><option key={z.id} value={z.id}>{z.name} · {z.formula}</option>)}</select><ChevronDown size={14}/></label></div><div className="chart-meta"><span><span className="legend-dot"/> {selectedZone?.gas_name||'Gas'} concentration</span><span>Unit: <strong>{selectedZone?.unit||'—'}</strong></span></div><div className="chart-area">{chart.length<2?<div className="chart-empty"><Activity size={21}/><span>{state.simulator?'Collecting readings…':'Start the simulation to generate live readings.'}</span></div>:<ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{top:10,right:12,left:0,bottom:0}}><CartesianGrid stroke="#edf0f4" vertical={false}/><XAxis dataKey="time" tick={{fontSize:10,fill:'#8993a2'}} tickLine={false} axisLine={false} minTickGap={35}/><YAxis tick={{fontSize:10,fill:'#8993a2'}} tickLine={false} axisLine={false} width={42}/><Tooltip contentStyle={{border:'1px solid #e7ebf0',borderRadius:8,fontSize:12}} formatter={(v)=>[`${v} ${selectedZone?.unit||''}`,'Reading']}/>{selectedZone&&<><Line dataKey="value" type="monotone" stroke="#3275c7" strokeWidth={2.5} dot={false} activeDot={{r:4}} isAnimationActive={false}/></>}</LineChart></ResponsiveContainer>}</div><div className="chart-footer"><span><span className="tiny-line"/> Reading</span><span><span className="tiny-dash warning-line"/> Warning threshold {selectedZone?.warning_threshold??'—'} {selectedZone?.unit}</span><span><span className="tiny-dash critical-line"/> Critical threshold {selectedZone?.critical_threshold??'—'} {selectedZone?.unit}</span></div></div>
      <div className="panel notification-panel"><div className="panel-head"><div><h3>Notification history</h3><p className="muted">Warning and critical event notifications</p></div><span className="panel-count">{state.notifications.length}</span></div>{state.notifications.length===0?<div className="empty-state"><span className="empty-icon"><Bell size={19}/></span><strong>No notifications yet</strong><span>Incident alerts will appear here.</span></div>:<div className="notification-list">{state.notifications.slice(0,8).map(n=><div className="notification-row" key={n.id}><span className={`notif-icon ${n.mode==='real'?'real':'mock'}`}>{n.mode==='real'?<Radio size={15}/>:<Bell size={15}/>}</span><div className="notif-main"><div className="notif-title"><strong>{n.zone_name} · {n.gas_name} · {n.event_severity}</strong><span className={`notif-status ${n.status==='FAILED'?'failed':''}`}>{n.status.replaceAll('_',' ')}</span></div><p>{n.reading} {n.unit} <span>·</span> {dateTime(n.created_at)}</p><small className="notif-message">{n.message}</small><small>{n.mode==='mock'?'Mock only · no SMS sent':n.provider_message_id?`Provider accepted · ID ${n.provider_message_id}`:'Provider response recorded'}</small></div></div>)}</div>}</div></section>
      <section className="panel incidents-panel"><div className="panel-head"><div><h3>Incident history</h3><p className="muted">Review events and record operator actions.</p></div><span className="panel-count">{state.incidents.length} records</span></div>{state.incidents.length===0?<div className="incident-empty"><ShieldCheck size={18}/><span>No incidents recorded. Zone events will be listed here.</span></div>:<div className="table-scroll"><table><thead><tr><th>ZONE / GAS</th><th>SEVERITY</th><th>READING</th><th>THRESHOLD</th><th>CREATED</th><th>STATE</th><th>NOTIFICATION</th><th>ACTIONS</th></tr></thead><tbody>{state.incidents.map(i=><tr key={i.incident_id}><td><strong>{i.zone_name}</strong><span className="table-sub">{i.gas_name} ({i.formula})</span></td><td><span className={`severity-tag ${i.severity.toLowerCase()}`}><span/>{i.severity}</span></td><td><strong>{i.reading}</strong> <span className="muted">{i.unit}</span></td><td>{i.display_threshold} <span className="muted">{i.unit}</span></td><td>{dateTime(i.created_at)}</td><td><span className={`state-tag ${i.state.toLowerCase()}`}>{i.state}</span>{i.acknowledged_at&&<span className="table-sub">Acknowledged {dateTime(i.acknowledged_at)}</span>}{i.resolved_at&&<span className="table-sub">Resolved {dateTime(i.resolved_at)}</span>}</td><td>{i.notification_status?<span className={`notif-status ${i.notification_status==='FAILED'?'failed':''}`}>{i.notification_mode==='mock'?'MOCK · ':''}{i.notification_status.replaceAll('_',' ')}</span>:<span className="muted">—</span>}</td><td><div className="row-actions"><button className="text-button" disabled={!canOperate||i.state!=='OPEN'||!!busy} onClick={()=>requestAction(`ack-${i.incident_id}`,`/api/incidents/${i.incident_id}/acknowledge`,{actor:'Operator'})}><Check size={13}/> Acknowledge</button><button className="text-button resolve" disabled={!canOperate||i.state==='RESOLVED'||!!busy} onClick={()=>requestAction(`res-${i.incident_id}`,`/api/incidents/${i.incident_id}/resolve`,{actor:'Operator'})}><ShieldCheck size={13}/> Resolve</button></div></td></tr>)}</tbody></table></div>}</section>
    </main>
  </div>
}

function ZoneCard({zone}:{zone:Zone}) {
  const status=zone.status.toLowerCase()
  const delta=zone.current_reading!=null&&zone.previous_reading!=null?zone.current_reading-zone.previous_reading:null
  const Icon=status==='critical'?Siren:status==='warning'?AlertTriangle:zone.id===2?Flame:zone.id===4?Wind:Activity
  return <article className={`zone-card ${status}`}><div className="zone-top"><div className={`gas-icon ${status}`}><Icon size={18}/></div><span className={`status-pill ${status}`}><span/>{zone.status}</span></div><div className="zone-ident"><h4>{zone.name}</h4><span>{zone.gas_name} <b>·</b> {zone.formula}</span></div><div className="reading-row"><div className="reading-value">{zone.current_reading==null?'—':Number(zone.current_reading).toFixed(2)}<span>{zone.unit}</span></div>{delta!==null&&<div className={`trend ${delta>0?'up':delta<0?'down':''}`}>{delta>0?'↑':delta<0?'↓':'→'} {Math.abs(delta).toFixed(2)}<span>vs prior</span></div>}</div><div className="thresholds"><div><span>Warning</span><strong>{zone.warning_threshold} <small>{zone.unit}</small></strong></div><div><span>Critical</span><strong>{zone.critical_threshold} <small>{zone.unit}</small></strong></div></div><div className="zone-bottom"><span>{zone.active_incident_id?<><span className="active-dot"/> Active incident</>:'Current status from latest reading'}</span><span>{zone.last_updated?new Date(zone.last_updated).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'No readings'}</span></div></article>
}
