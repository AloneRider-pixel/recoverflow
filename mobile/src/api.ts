import * as SecureStore from "expo-secure-store";
export const API_URL=(process.env.EXPO_PUBLIC_API_URL||"https://recoverflow-7vnr.onrender.com").replace(/\/$/,"");
const TOKEN_KEY="recoverflow_access_token";
async function request(path:string,options:RequestInit={}){
  const token=await SecureStore.getItemAsync(TOKEN_KEY);
  const headers=new Headers(options.headers||{}); headers.set("Content-Type","application/json");
  if(token) headers.set("Authorization","Bearer "+token);
  const response=await fetch(API_URL+path,{...options,headers});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(data.detail||"Something went wrong");
  return data;
}
export async function login(email:string,password:string){const data=await request("/api/v1/auth/login",{method:"POST",body:JSON.stringify({email,password})});await SecureStore.setItemAsync(TOKEN_KEY,data.access_token);return data;}
export async function register(email:string,password:string,company_name:string){const data=await request("/api/v1/auth/register",{method:"POST",body:JSON.stringify({email,password,company_name})});await SecureStore.setItemAsync(TOKEN_KEY,data.access_token);return data;}
export async function logout(){await SecureStore.deleteItemAsync(TOKEN_KEY);}
export async function me(){return request("/api/v1/me");}
export async function dashboard(){return request("/api/v1/dashboard");}
export async function invoices(){return request("/api/v1/invoices");}
export async function createInvoice(payload:any){return request("/api/v1/invoices",{method:"POST",body:JSON.stringify(payload)});}
export async function markPaid(id:number){return request("/api/v1/invoices/"+id+"/mark-paid",{method:"POST"});}
export async function hasToken(){return !!(await SecureStore.getItemAsync(TOKEN_KEY));}