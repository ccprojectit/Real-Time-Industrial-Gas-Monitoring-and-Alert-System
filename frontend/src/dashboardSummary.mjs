export function calculateDashboardSummary(zones, incidents) {
  const uniqueZones = new Map(zones.map((zone) => [zone.id, zone]))
  const uniqueIncidents = new Map(incidents.map((incident) => [incident.incident_id, incident]))
  const zoneList = [...uniqueZones.values()]
  const incidentList = [...uniqueIncidents.values()]

  return {
    normal: zoneList.filter((zone) => zone.status === 'NORMAL').length,
    critical: zoneList.filter((zone) => zone.status === 'CRITICAL').length,
    openIncidents: incidentList.filter((incident) => incident.severity === 'CRITICAL' && incident.state !== 'RESOLVED').length,
  }
}
