export interface SummaryZone {
  id: number
  status: string
}

export interface SummaryIncident {
  incident_id: string
  severity: string
  state: string
}

export interface DashboardSummary {
  normal: number
  critical: number
  openIncidents: number
}

export function calculateDashboardSummary(
  zones: SummaryZone[],
  incidents: SummaryIncident[],
): DashboardSummary
