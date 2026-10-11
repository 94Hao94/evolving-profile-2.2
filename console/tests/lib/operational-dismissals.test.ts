import { describe, expect, it } from "vitest";
import * as operational from "@/lib/operational-overview";
import type { OperationalIncident, AttemptGroup } from "@/lib/operational-overview";

const api=operational as any;
const incident:OperationalIncident={id:"map:refresh",category:"map",severity:"warning",title:"同步未核验",detail:"等待更新",source:"map",at:"2026-10-08T02:00:00Z",action:"flow"};
const attempt:AttemptGroup={id:"attempt:r1",traceId:"trace-1",operation:"retain",scope:null,errorClass:"timeout",attemptCount:1,requestIds:["r1"],at:"2026-10-08T02:00:00Z",recovery:"unresolved",traceComplete:false,laterSuccessId:null};

describe("clear operational display without clearing audit evidence",()=>{
  it("hides current issues and historical groups and persists through JSON reload",()=>{
    const incidents=Object.freeze([Object.freeze({...incident})]);
    const state=api.dismissOperationalDisplay?.({},incidents,[attempt]);
    const reload=state && JSON.parse(JSON.stringify(state));
    expect(api.visibleOperationalIncidents?.(incidents,reload)).toEqual([]);
    expect(api.visibleOperationalAttempts?.([attempt],reload)).toEqual([]);
    expect(incidents).toHaveLength(1);
    expect(incidents[0].severity).toBe("warning");
  });
  it("keeps a fresh receipt or fresh failed attempt visible",()=>{
    const state=api.dismissOperationalDisplay?.({},[incident],[attempt]);
    const fresh={...incident,id:"operation:new-receipt",at:"2026-10-08T03:00:00Z"};
    const retry={...attempt,attemptCount:2,requestIds:["r1","r2"],at:"2026-10-08T03:00:00Z"};
    expect(api.visibleOperationalIncidents?.([incident,fresh],state)).toEqual([fresh]);
    expect(api.visibleOperationalAttempts?.([retry],state)).toEqual([retry]);
  });
  it("does not re-show the same stateful issue just because a polling timestamp changes",()=>{
    const state=api.dismissOperationalDisplay?.({},[incident],[]);
    expect(api.visibleOperationalIncidents?.([{...incident,at:"2026-10-08T03:00:00Z"}],state)).toEqual([]);
  });
  it("shows a changed issue count and a genuinely recurring issue after recovery",()=>{
    const current={...incident,count:4};
    const state=api.dismissOperationalDisplay?.({},[current],[]);
    const worse={...current,count:5};
    expect(api.visibleOperationalIncidents?.([worse],state)).toEqual([worse]);
    const recovered=api.reconcileOperationalDismissals?.(state,["map"]);
    expect(api.visibleOperationalIncidents?.([current],recovered)).toEqual([current]);
  });
  it("uses distinct keys for banks and fails open on invalid stored state",()=>{
    expect(api.operationalDismissalStorageKey?.("bank-a")).not.toEqual(api.operationalDismissalStorageKey?.("bank-b"));
    expect(api.parseOperationalDismissals?.('not-json')).toEqual({});
    expect(api.visibleOperationalIncidents?.([incident],api.parseOperationalDismissals?.('{"bad":"shape"}'))).toEqual([incident]);
  });
});
