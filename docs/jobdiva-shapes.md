# JobDiva API — discovered response shapes

Probed 2026-08-28T18:22:14.596598+00:00 against `api.jobdiva.com`.

The V2 Swagger declares 260 of 431 endpoints as `IBiData` — an object with no
properties — so these shapes were discovered by calling the API, not read from
the spec. Structure only; raw payloads are in the gitignored `.raw.json`.

## Resolved unknowns

- **Auth header format:** `Authorization: <token>` (`raw`)
- **JobDetail structured requirements:** present: `skills` · missing: `excludedskills`, `experience`, `securityclearance`, `description`
  → structured job-side scoring is viable

## Endpoint shapes

### `/apiv2/bi/ApiLimits`
Rows: 0

```
[]
```

### `/apiv2/bi/CandidateApplicationRecords`
Params: `{'fromDate': '05/30/2026 18:22:14', 'toDate': '08/28/2026 18:22:14', 'pageNumber': 1, 'pageSize': 5}`

**FAILED** — GET /apiv2/bi/CandidateApplicationRecords failed (429): Request Limit Exceeded

### `/apiv2/bi/OpenJobsList`

```
{message: str, data: [{JOBID: str, JOBDIVANO: str, OPTIONALREFERENCENO: str, DIVISIONID: str, DIVISIONNAME: str, PRIMARYRECRUITERID: str, PRIMARYSALESID: str, COMPANYID: str, COMPANYNAME: str, PRIMARYOWNERID: str, CONTACTID: str, CONTACTNAME: str, ...}] x1}
```

Keys seen: `BILLFREQUENCY`, `BILLRATEMAX`, `BILLRATEMIN`, `CITY`, `COMPANYID`, `COMPANYNAME`, `CONTACTID`, `CONTACTNAME`, `CURRENCY`, `DATEUPDATED`, `DIVISIONID`, `DIVISIONNAME`, `ENDDATE`, `FILLS`, `GENERAL_PROF_SPEC`, `HealthCare`, `ISSUEDATE`, `JOBDIVANO`, `JOBID`, `JOBSTATUS`, `MAXALLOWEDSUBMITTALS`, `ONSITE_FLEXIBILITY`, `OPENINGS`, `OPTIONALREFERENCENO`, `PAYFREQUENCY`, `PAYRATEMAX`, `PAYRATEMIN`, `POSITIONTYPE`, `PRIMARYOWNERID`, `PRIMARYRECRUITERID`, `PRIMARYSALESID`, `PRIORITY`, `REMOTE_PERCENTAGE`, `STARTDATE`, `STATE`, `TITLE`, `ZIPCODE`, `data`, `message`

### `/apiv2/bi/JobDetail`
Params: `{'jobId': 28097080}`

```
{message: str, data: [{ID: str, DATEISSUED: str, DATEUPDATED: str, DATEUSERFIELDUPDATED: str, DATESTATUSUPDATED: str, JOBSTATUS: str, CUSTOMERID: str, COMPANYID: str, COMPANYNAME: str, ADDRESS1: str, ADDRESS2: str, UPDATEDBY: str, ...}] x1}
```

Keys seen: `ADDRESS1`, `ADDRESS2`, `AGENT_SEARCH_TITLE`, `BACKGROUND_CHECK`, `BILLRATEMAX`, `BILLRATEMIN`, `BILLRATEPER`, `CATALOGACTIVE`, `CATALOGBILLRATEHIGH`, `CATALOGBILLRATELOW`, `CATALOGBILLRATEPER`, `CATALOGCATEGORY`, `CATALOGCOMPANYID`, `CATALOGEFFECTIVEDATE`, `CATALOGEXPIRATIONDATE`, `CATALOGNAME`, `CATALOGNOTES`, `CATALOGPAYRATEHIGH`, `CATALOGPAYRATELOW`, `CATALOGPAYRATEPER`, `CATALOGREFNO`, `CATALOGTITLE`, `CERTIFICATIONS`, `CITY`, `COMPANYID`, `COMPANYNAME`, `CONTACTFIRSTNAME`, `CONTACTID`, `CONTACTLASTNAME`, `COUNTRY`, `CREATED_BY`, `CRITERIA_DEGREE`, `CURRENCY`, `CUSTOMERID`, `DATEISSUED`, `DATESTATUSUPDATED`, `DATEUPDATED`, `DATEUSERFIELDUPDATED`, `DIVISION`, `DIVISIONID`, `DIVISIONNAME`, `DRUG_TEST`, `EEOC_FEDERAL_SECTOR_OCCUPATION`, `ENDDATE`, `FACILITY`, `FEE`, `FEE_TYPE`, `FILLS`, `GENERAL_PROF_SPEC`, `HARVEST`, `HealthCare`, `ID`, `JOBCATALOGID`, `JOBDESCRIPTION`, `JOBDIVANO`, `JOBSCHEDULE`, `JOBSTATUS`, `JOBTITLE`, `JOB_CATEGORY`, `MAXALLOWEDSUBMITTALS`

### `/apiv2/bi/PipelineStages`

```
{message: str, data: []}
```

Keys seen: `data`, `message`

### `/apiv2/bi/ActionTypeList`

```
{message: str, data: [{ID: str, TYPE: str, NAME: str, ACTIVE: str}] x59}
```

Keys seen: `ACTIVE`, `ID`, `NAME`, `TYPE`, `data`, `message`

### `/apiv2/getRejectReasons`

```
{message: str, data: []}
```

Keys seen: `data`, `message`

### `/apiv2/jobdiva/ScreenerQuestions`
Rows: 0

```
[]
```
