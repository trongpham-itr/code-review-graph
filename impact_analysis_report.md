# Impact Analysis Report — Direct vs Indirect Impact per Repo

Mỗi repo chọn function có **số operation bị ảnh hưởng trực tiếp nhiều nhất**.

- **Trực tiếp (Direct, depth=1)** — resolver gọi thẳng function này, không qua tầng trung gian
- **Gián tiếp (Indirect, depth≥2)** — nằm sâu hơn trong call chain; cột *Hops* cho biết số tầng

---


## btcy-biocare-backend-device_management_api

**Function:** `assignCurrentDevices` in `controllers/deviceHistory/deviceHistoryCommand.js`  
**Affected:** 4 tổng (4 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.createDevice` |
| `Mutation.createDevices` |
| `Mutation.updateDevice` |
| `Mutation.updateDevices` |


## btcy-bioflux-backend-admin_api

**Function:** `createAppInfo` in `controllers/appInfo/appInfoCommand.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.createAppInfo` |


## btcy-bioflux-backend-ai_api

**Function:** `isCountRateLimited` in `utils/rateLimit/countRateLimit.js`  
**Affected:** 4 tổng (4 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Query.holterBeatEventsCount` |
| `Query.holterEventsDailyCount` |
| `Query.holterRhythmEventsCount` |
| `Query.holterRrHeatMapCount` |


## btcy-bioflux-backend-auth_api

**Function:** `login` in `controllers/user/authentication.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.login` |


## btcy-bioflux-backend-billing_api

**Function:** `generateInvoices` in `controllers/invoice/invoiceCommand.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.generateInvoices` |


## btcy-bioflux-backend-biodirect_api

**Function:** `initiateBiofluxDirectStudy` in `controllers/biodirect/biodirectCommand.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.initiateBiofluxDirectStudy` |


## btcy-bioflux-backend-call_center_active_time_tracking

**Function:** `useGraphqlHandler` in `app/utils/useHttpHandler.js`  
**Affected:** 2 tổng (2 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.callCenterActiveTimeTracking` |
| `Query.getActiveTimeReport` |


## btcy-bioflux-backend-call_center_api

**Function:** `closeEvent` in `controllers/event/eventCommand.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.closeEvent` |


## btcy-bioflux-backend-clinic_api

**Function:** `getBiocareUser` in `controllers/biocareUser/biocareUserQuery.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Query.biocareUser` |


## btcy-bioflux-backend-common_reference

**Function:** `getUsersOfFacility` in `app/resolvers/facility.js`  
**Affected:** 2 tổng (2 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Facility.adminUser` |
| `Facility.users` |


## btcy-bioflux-backend-report_api

**Function:** `getDeviceEventsV2` in `controllers/ecgEvent/eventQuery.js`  
**Affected:** 1 tổng (1 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Query.deviceEventsV2` |


## btcy-bioflux-backend-sales_api

**Function:** `loadRoleStats` in `app/resolvers/user.js`  
**Affected:** 4 tổng (4 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `User.averageStudyLength` |
| `User.deviceUtilizationGoal` |
| `User.facilityRanking` |
| `User.summarySalesStatistic` |


## btcy-bioflux-backend-study_api

**Function:** `createSelectedFields` in `utils/others/selectFields.js`  
**Affected:** 27 tổng (3 trực tiếp · 24 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Study.info` |
| `Study.linkedStudies` |
| `Study.patientReturn` |

#### Gián tiếp

| GraphQL Operation | Hops |
|---|---|
| `Mutation.appendStudyLog` | 2 |
| `Mutation.assignDeviceToStudy` | 2 |
| `Mutation.updateStudy` | 2 |
| `Query.callCenterStudies` | 2 |
| `Query.clinicStudies` | 2 |
| `Query.deletedStudies` | 2 |
| `Query.deletedStudy` | 2 |
| `Query.flaggedStudies` | 2 |
| `Query.getStudyByApp` | 2 |
| `Query.insuranceStudies` | 2 |
| `Query.lastPatientStudy` | 2 |
| `Query.patientStudies` | 2 |
| `Query.studies` | 2 |
| `Query.study` | 2 |
| `Query.studyByPatientInfo` | 2 |
| `Query.studyByReferenceCode` | 2 |
| `Mutation.updateStudyPatientContact` | 3 |
| `Mutation.updateStudySecondaryContact` | 3 |
| `Mutation.updateStudyDiagnosis` | 4 |
| `Mutation.updateStudyInterprettingPhysician` | 4 |
| `Mutation.updateStudyPatientInformation` | 4 |
| `Mutation.updateStudyPatientInsurance` | 4 |
| `Mutation.updateStudyPatientMedicalHistory` | 4 |
| `Mutation.updateStudyReferringPhysician` | 4 |


## btcy-bioflux-backend-support_api

**Function:** `assignCurrentDevices` in `controllers/deviceHistory/deviceHistoryCommand.js`  
**Affected:** 15 tổng (15 trực tiếp · 0 gián tiếp)

#### Trực tiếp

| GraphQL Operation |
|---|
| `Mutation.cancelSameOrgDeviceTransfer` |
| `Mutation.completeDeviceTransfer` |
| `Mutation.completeRmaTicket` |
| `Mutation.completeSameOrgDeviceTransfer` |
| `Mutation.createDeviceTransfer` |
| `Mutation.createRmaTicket` |
| `Mutation.createShipmentDevice` |
| `Mutation.deleteShipmentDevice` |
| `Mutation.markAsShippedDevice` |
| `Mutation.updateOperationForm` |
| `Mutation.updateRmaTicket` |
| `Mutation.updateShipmentDevice` |
| `Mutation.updateTransferShipmentDevice` |
| `Mutation.updateTravelerForm` |
| `Mutation.upsertTravelerForm` |

