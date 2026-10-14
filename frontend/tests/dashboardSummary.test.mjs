import assert from 'node:assert/strict'
import test from 'node:test'
import { calculateDashboardSummary } from '../src/dashboardSummary.mjs'

const zone = (id, status) => ({ id, status })
const incident = (id, state, severity = 'CRITICAL') => ({ incident_id: id, state, severity })

test('critical reading with an open incident counts as one critical zone and one open incident', () => {
  assert.deepEqual(
    calculateDashboardSummary([zone(1, 'CRITICAL')], [incident('i-1', 'OPEN'), incident('i-1', 'OPEN')]),
    { normal: 0, critical: 1, openIncidents: 1 },
  )
})

test('critical reading remains critical after its incident is resolved', () => {
  assert.deepEqual(
    calculateDashboardSummary([zone(1, 'CRITICAL')], [incident('i-1', 'RESOLVED')]),
    { normal: 0, critical: 1, openIncidents: 0 },
  )
})

test('normal reading with a resolved incident counts as normal and not open', () => {
  assert.deepEqual(
    calculateDashboardSummary([zone(1, 'NORMAL')], [incident('i-1', 'RESOLVED')]),
    { normal: 1, critical: 0, openIncidents: 0 },
  )
})

test('all resolved incidents produce zero open incidents without duplicate counting', () => {
  assert.deepEqual(
    calculateDashboardSummary(
      [zone(1, 'CRITICAL'), zone(1, 'CRITICAL'), zone(2, 'NORMAL')],
      [incident('i-1', 'RESOLVED'), incident('i-1', 'RESOLVED'), incident('i-2', 'RESOLVED')],
    ),
    { normal: 1, critical: 1, openIncidents: 0 },
  )
})

test('preserved historical records do not affect current counts or open incidents', () => {
  assert.deepEqual(
    calculateDashboardSummary(
      [zone(1, 'NORMAL')],
      [incident('old-event', 'HISTORICAL', 'HISTORICAL')],
    ),
    { normal: 1, critical: 0, openIncidents: 0 },
  )
})
