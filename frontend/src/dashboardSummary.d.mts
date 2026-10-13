export interface SummaryZone {
  id: number
  status: string
}

export interface SummaryIncident {
  incident_id: string
  state: string
}

export interface DashboardSummary {
  normal: number
  warning: number
  critical: number
  openIncidents: number
}

export function calculateDashboardSummary(
  zones: SummaryZone[],
  incidents: SummaryIncident[],
): DashboardSummary
