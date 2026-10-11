import {afterEach,beforeEach,expect,it,vi} from "vitest";
import {mkdtemp,mkdir,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
let root:string;
beforeEach(async()=>{
 root=await mkdtemp(path.join(tmpdir(),"ep-model-identity-"));
 vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT",root);vi.resetModules();
 await mkdir(path.join(root,"config"));await mkdir(path.join(root,"profiles"));
 await writeFile(path.join(root,"profiles/evolving-profile-api.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID=intfloat/multilingual-e5-small\nEVOLVING_PROFILE_API_RERANKER_PROVIDER=rrf\n');
 await writeFile(path.join(root,"config/retrieval-models.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID="sentence-transformers/all-MiniLM-L6-v2"\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_PATH="/models/minilm/onnx/model.onnx"\n');
 await writeFile(path.join(root,"config/runtime-settings.json"),JSON.stringify({retrieval_models:{embedding:{model:"intfloat/multilingual-e5-small",profile_id:"embedding-default"},embedding_profiles:[{model:"intfloat/multilingual-e5-small",profile_id:"embedding-default",dimensions:384,provider:"onnx",mode:"local",local_path:""}]}}));
});
afterEach(async()=>{vi.unstubAllEnvs();await rm(root,{recursive:true,force:true})});
it("shows effective overlay identity and stale profile separately without replacing the saved profile",async()=>{
 const {GET}=await import("@/app/api/evolving-profile/retrieval-model-settings/route");
 const result=await (await GET()).json();
 expect(result.embedding.model).toBe("sentence-transformers/all-MiniLM-L6-v2");
 expect(result.embedding.local_path).toBe("/models/minilm");
 expect(result.embedding.profile_identity).toBe("drifted");
 expect(result.embedding_profiles[0].model).toBe("intfloat/multilingual-e5-small");
 expect(result.reranker).toMatchObject({enabled:false,status:"configured_but_inactive",profile_identity:"inactive"});
});
it.each([
 {provider:"local",overlay:'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER="local"\nEVOLVING_PROFILE_API_EMBEDDINGS_LOCAL_MODEL="BAAI/bge-m3"\n',want:{provider:"local",mode:"local",model:"BAAI/bge-m3",local_path:"",dimensions:null}},
 {provider:"openai",overlay:'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER="openai"\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_MODEL="text-embedding-3-large"\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_DIMENSIONS="1024"\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_BASE_URL="https://example.invalid/v1"\n',want:{provider:"openai",mode:"api",model:"text-embedding-3-large",local_path:"",dimensions:1024,base_url:"https://example.invalid/v1"}},
])("ignores old ONNX identity after base/overlay switch to $provider",async({overlay,want})=>{
 await writeFile(path.join(root,"profiles/evolving-profile-api.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER=onnx\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID=old-onnx\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_PATH=/old/onnx/model.onnx\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_DIMENSIONS=384\n');
 await writeFile(path.join(root,"config/retrieval-models.env"),overlay);
 const {GET}=await import("@/app/api/evolving-profile/retrieval-model-settings/route");
 const result=await (await GET()).json();
 expect(result.embedding).toMatchObject(want);expect(result.embedding.effective_configuration).toMatchObject(want);
});
it("does not fill missing API dimensions from ONNX defaults even when the model label matches",async()=>{
 await writeFile(path.join(root,"profiles/evolving-profile-api.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER=onnx\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID=shared-model-label\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_PATH=/old/onnx/model.onnx\nEVOLVING_PROFILE_API_EMBEDDINGS_ONNX_DIMENSIONS=384\n');
 await writeFile(path.join(root,"config/retrieval-models.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER=openai\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_MODEL=shared-model-label\n');
 await writeFile(path.join(root,"config/runtime-settings.json"),JSON.stringify({retrieval_models:{embedding:{provider:"openai",mode:"api",model:"shared-model-label"}}}));
 const {GET}=await import("@/app/api/evolving-profile/retrieval-model-settings/route");
 const result=await (await GET()).json();expect(result.embedding.dimensions).toBeNull();expect(result.embedding.local_path).toBe("");
});
it("masks API credentials in generated profile copies as well as the active configuration",async()=>{
 await writeFile(path.join(root,"config/retrieval-models.env"),'EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER=openai\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_MODEL=fixture-model\nEVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_API_KEY=fixture-secret-not-real\n');
 await writeFile(path.join(root,"config/runtime-settings.json"),JSON.stringify({retrieval_models:{embedding:{provider:"openai",mode:"api",model:"fixture-model"}}}));
 const {GET}=await import("@/app/api/evolving-profile/retrieval-model-settings/route");const result=await (await GET()).json();
 expect(result.embedding_profiles[0].api_key).toBe("••••real");expect(JSON.stringify(result)).not.toContain("fixture-secret-not-real");
});
