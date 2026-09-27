import React,{useEffect,useState} from "react";
import {ActivityIndicator,Alert,KeyboardAvoidingView,Platform,Pressable,SafeAreaView,ScrollView,StyleSheet,Text,TextInput,View,Linking} from "react-native";
import {LinearGradient} from "expo-linear-gradient";
import {StatusBar} from "expo-status-bar";
import {colors,radius} from "./src/theme";
import * as api from "./src/api";
import {attachNotificationListeners,registerPushNotifications} from "./src/notifications";

type Tab="home"|"invoices"|"add"|"customers"|"more"|"recurring";
type Invoice={id:number;invoice_number:string;customer_name:string;phone?:string;amount:string;paid_amount:string;balance:string;due_date:string;status:string;days_overdue:number;aging_bucket:string};

function Loader(){return <View style={styles.loader}><ActivityIndicator color={colors.cyan}/><Text style={styles.muted}>Loading RecoverFlow…</Text></View>}
function Money({value,large=false}:{value:string|number;large?:boolean}){
  const n=Number(value);
  return <Text style={[styles.money,large&&styles.moneyLarge]}>₹{isNaN(n)?String(value):n.toLocaleString("en-IN",{maximumFractionDigits:2})}</Text>;
}
function Field(props:any){return <View style={styles.fieldWrap}><Text style={styles.label}>{props.label}</Text><TextInput {...props} style={styles.input} placeholderTextColor="#61788B"/></View>}

function Auth({onDone}:{onDone:()=>void}){
  const [mode,setMode]=useState<"login"|"register">("login");
  const [email,setEmail]=useState(""); const [password,setPassword]=useState(""); const [company,setCompany]=useState(""); const [loading,setLoading]=useState(false);
  async function submit(){
    if(!email||password.length<8||(mode==="register"&&!company)){Alert.alert("Check your details","Enter valid account information.");return}
    setLoading(true);
    try{if(mode==="login")await api.login(email,password);else await api.register(email,password,company);onDone();}
    catch(e:any){Alert.alert("Could not continue",e?.message||"Please try again.");}
    finally{setLoading(false);}
  }
  return <SafeAreaView style={styles.auth}>
    <StatusBar style="light"/>
    <LinearGradient colors={["#0E2841","#07111F"]} style={StyleSheet.absoluteFill}/>
    <KeyboardAvoidingView style={styles.center} behavior={Platform.OS==="ios"?"padding":undefined}>
      <ScrollView contentContainerStyle={styles.authScroll}>
        <View style={styles.brandRow}><View style={styles.logo}><Text style={styles.logoText}>↗</Text></View><Text style={styles.brand}>RecoverFlow</Text></View>
        <Text style={styles.kicker}>RECEIVABLES OPERATIONS</Text>
        <Text style={styles.hero}>Get paid.{"\n"}Without the chase.</Text>
        <Text style={styles.authSub}>Invoices, customer follow-ups and collections in one focused workspace.</Text>
        <View style={styles.card}>
          <View style={styles.segment}>
            <Pressable onPress={()=>setMode("login")} style={[styles.segmentBtn,mode==="login"&&styles.segmentActive]}><Text style={styles.segmentText}>Sign in</Text></Pressable>
            <Pressable onPress={()=>setMode("register")} style={[styles.segmentBtn,mode==="register"&&styles.segmentActive]}><Text style={styles.segmentText}>Create account</Text></Pressable>
          </View>
          {mode==="register"&&<Field label="Company name" value={company} onChangeText={setCompany} placeholder="Your business" autoCapitalize="words"/>}
          <Field label="Work email" value={email} onChangeText={setEmail} placeholder="you@company.com" keyboardType="email-address" autoCapitalize="none"/>
          <Field label="Password" value={password} onChangeText={setPassword} placeholder="Minimum 8 characters" secureTextEntry/>
          <Pressable onPress={submit} disabled={loading} style={styles.primary}><Text style={styles.primaryText}>{loading?"Working…":mode==="login"?"Sign in":"Create workspace"}</Text></Pressable>
        </View>
        <Text style={styles.foot}>Secure access · Shared RecoverFlow backend</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  </SafeAreaView>;
}

function Home({go}:{go:(tab:Tab)=>void}){
  const [data,setData]=useState<any>(null); const [loading,setLoading]=useState(true);
  useEffect(()=>{api.dashboard().then(setData).catch(()=>{}).finally(()=>setLoading(false));},[]);
  if(loading)return <Loader/>;
  return <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
    <Text style={styles.kicker}>TODAY</Text><Text style={styles.title}>Collection control center</Text><Text style={styles.muted}>See where cash is stuck and what needs attention.</Text>
    <LinearGradient colors={["#123A58","#0B1728"]} style={styles.heroCard}>
      <Text style={styles.cardKicker}>OUTSTANDING</Text><Money value={data?.outstanding||0} large/>
      <View style={styles.heroRow}>
        <Metric label="Overdue" value={data?.overdue||0}/>
        <Metric label="Paid" value={data?.paid||0}/>
        <Metric label="Invoices" value={String(data?.invoice_count||0)} text/>
      </View>
    </LinearGradient>
    <View style={styles.statsGrid}><Stat label="Due today" value={data?.due_today||"0"}/><Stat label="Customers" value={String(data?.customer_count||0)}/></View>
    <View style={styles.actionStack}>
      <Action title="Open collection queue" subtitle="Review invoices and payment actions." onPress={()=>go("invoices")} icon="→"/>
      <Action title="Customer portfolio" subtitle="See outstanding exposure by customer." onPress={()=>go("customers")} icon="◉"/>
      <Action title="Recurring billing" subtitle="Automate repeat invoice creation." onPress={()=>go("recurring")} icon="↻"/>
    </View>
  </ScrollView>;
}
function Metric({label,value,text=false}:{label:string;value:string;text?:boolean}){return <View><Text style={styles.smallMuted}>{label}</Text>{text?<Text style={styles.whiteBig}>{value}</Text>:<Money value={value}/>}</View>}
function Stat({label,value}:{label:string;value:string}){return <View style={styles.stat}><Text style={styles.statLabel}>{label}</Text><Money value={value} large/><Text style={styles.statTone}>Current position</Text></View>}
function Action({title,subtitle,onPress,icon}:{title:string;subtitle:string;onPress:()=>void;icon:string}){return <Pressable onPress={onPress} style={styles.actionCard}><View style={{flex:1}}><Text style={styles.actionTitle}>{title}</Text><Text style={styles.muted}>{subtitle}</Text></View><Text style={styles.arrow}>{icon}</Text></Pressable>}

function InvoiceRow({item,onRefresh}:{item:Invoice;onRefresh:()=>void}){
  const [busy,setBusy]=useState(false);
  async function run(fn:()=>Promise<any>){setBusy(true);try{await fn();onRefresh();}catch(e:any){Alert.alert("Action unavailable",e?.message||"Please try again.");}finally{setBusy(false);}}
  return <View style={styles.invoice}>
    <View style={styles.invoiceTop}><View style={{flex:1}}><Text style={styles.invoiceCustomer}>{item.customer_name}</Text><Text style={styles.invoiceNo}>{item.invoice_number} · due {item.due_date}</Text></View><Money value={item.balance}/></View>
    <View style={styles.invoiceBottom}><Text style={[styles.badge,item.days_overdue>0&&styles.badgeDanger]}>{item.days_overdue>0?item.days_overdue+"d overdue":item.status}</Text>
      <View style={styles.rowActions}>
        {item.balance!=="0"&&<Pressable disabled={busy} onPress={()=>run(async()=>{const r=await api.createPaymentLink(item.id);await Linking.openURL(r.url);})}><Text style={styles.link}>Pay link</Text></Pressable>}
        {item.balance!=="0"&&item.phone&&<Pressable disabled={busy} onPress={()=>run(()=>api.sendWhatsapp(item.id))}><Text style={styles.link}>WhatsApp</Text></Pressable>}
        {item.balance!=="0"&&<Pressable disabled={busy} onPress={()=>run(()=>api.markPaid(item.id))}><Text style={styles.link}>{busy?"Updating…":"Mark paid"}</Text></Pressable>}
      </View>
    </View>
  </View>;
}

function Invoices(){
  const [items,setItems]=useState<Invoice[]>([]); const [loading,setLoading]=useState(true);
  async function load(){setLoading(true);try{setItems(await api.invoices())}catch{}finally{setLoading(false)}}
  useEffect(()=>{load();},[]);
  if(loading)return <Loader/>;
  return <FlatListScreen title="Invoices" kicker="LEDGER" subtitle={items.length+" receivables in your workspace."}><View>{items.map(x=><InvoiceRow key={x.id} item={x} onRefresh={load}/>)}{items.length===0&&<Empty title="No invoices yet" text="Create your first receivable from the Add tab."/>}</View></FlatListScreen>;
}
function FlatListScreen({children,title,kicker,subtitle}:{children:any;title:string;kicker:string;subtitle:string}){return <ScrollView style={styles.screen} contentContainerStyle={styles.content}><Text style={styles.kicker}>{kicker}</Text><Text style={styles.title}>{title}</Text><Text style={styles.muted}>{subtitle}</Text><View style={{marginTop:18}}>{children}</View></ScrollView>}
function Empty({title,text}:{title:string;text:string}){return <View style={styles.empty}><Text style={styles.emptyTitle}>{title}</Text><Text style={styles.muted}>{text}</Text></View>}

function Customers(){
  const [items,setItems]=useState<any[]>([]); const [loading,setLoading]=useState(true);
  useEffect(()=>{api.customers().then(setItems).catch(()=>{}).finally(()=>setLoading(false));},[]);
  if(loading)return <Loader/>;
  return <FlatListScreen title="Customers" kicker="CUSTOMER PORTFOLIO" subtitle="See who owes you and where the exposure sits.">
    {items.length?items.map((item,i)=><View key={item.name+i} style={styles.customer}><View style={styles.invoiceTop}><View style={{flex:1}}><Text style={styles.invoiceCustomer}>{item.name}</Text><Text style={styles.invoiceNo}>{item.invoice_count} invoice(s){item.phone?" · "+item.phone:""}</Text></View><Money value={item.outstanding}/></View><View style={styles.invoiceBottom}><Text style={[styles.badge,item.overdue!=="0"&&styles.badgeDanger]}>{item.overdue!=="0"?"Overdue "+item.overdue:"Current"}</Text><Text style={styles.smallMuted}>Exposure</Text></View></View>):<Empty title="No customers yet" text="Customers appear as you add invoices."/>}
  </FlatListScreen>;
}

function AddInvoice({onDone}:{onDone:()=>void}){
  const [number,setNumber]=useState("");const [customer,setCustomer]=useState("");const [phone,setPhone]=useState("");const [amount,setAmount]=useState("");const [due,setDue]=useState(new Date().toISOString().slice(0,10));const [gstin,setGstin]=useState("");const [loading,setLoading]=useState(false);
  async function submit(){
    if(!number||!customer||!amount){Alert.alert("Missing details","Add invoice number, customer and amount.");return}
    setLoading(true);
    try{await api.createInvoice({invoice_number:number,customer_name:customer,phone,amount,paid_amount:"0",issue_date:new Date().toISOString().slice(0,10),due_date:due,gstin});Alert.alert("Invoice created","The receivable is now in your collection queue.");onDone();}
    catch(e:any){Alert.alert("Could not create invoice",e?.message||"Please check the values.");}
    finally{setLoading(false);}
  }
  return <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS==="ios"?"padding":undefined}><ScrollView contentContainerStyle={styles.content}><Text style={styles.kicker}>NEW RECEIVABLE</Text><Text style={styles.title}>Add invoice</Text><Text style={styles.muted}>Create the receivable RecoverFlow will track.</Text><View style={[styles.card,{marginTop:18}]}>
    <Field label="Invoice number" value={number} onChangeText={setNumber} placeholder="INV-2048"/>
    <Field label="Customer" value={customer} onChangeText={setCustomer} placeholder="Customer name"/>
    <Field label="Phone / WhatsApp" value={phone} onChangeText={setPhone} placeholder="919876543210" keyboardType="phone-pad"/>
    <Field label="Amount (INR)" value={amount} onChangeText={setAmount} placeholder="84000" keyboardType="decimal-pad"/>
    <Field label="Due date" value={due} onChangeText={setDue} placeholder="YYYY-MM-DD"/>
    <Field label="Customer GSTIN (optional)" value={gstin} onChangeText={setGstin} placeholder="22AAAAA0000A1Z5" autoCapitalize="characters"/>
    <Pressable onPress={submit} disabled={loading} style={styles.primary}><Text style={styles.primaryText}>{loading?"Creating…":"Create invoice"}</Text></Pressable>
  </View></ScrollView></KeyboardAvoidingView>;
}

function Recurring({onBack}:{onBack:()=>void}){
  const [items,setItems]=useState<any[]>([]); const [name,setName]=useState(""); const [amount,setAmount]=useState(""); const [cadence,setCadence]=useState("monthly"); const [loading,setLoading]=useState(true);
  async function load(){setLoading(true);try{setItems(await api.recurring())}catch{}finally{setLoading(false)}}
  useEffect(()=>{load();},[]);
  async function create(){if(!name||!amount){Alert.alert("Missing details","Add customer and amount.");return}try{await api.createRecurring({customer_name:name,amount,cadence,next_issue_date:new Date().toISOString().slice(0,10),due_days:7});setName("");setAmount("");Alert.alert("Schedule created","The recurring billing schedule is active.");load();}catch(e:any){Alert.alert("Could not create",e?.message||"Please try again.");}}
  return <ScrollView style={styles.screen} contentContainerStyle={styles.content}><Pressable onPress={onBack} style={{marginBottom:18}}><Text style={styles.link}>← More</Text></Pressable><Text style={styles.kicker}>AUTOMATED BILLING</Text><Text style={styles.title}>Recurring invoices</Text><Text style={styles.muted}>Turn repeat customer billing into a scheduled workflow.</Text>
    <View style={[styles.card,{marginTop:18}]}><Field label="Customer" value={name} onChangeText={setName} placeholder="Customer name"/><Field label="Amount (INR)" value={amount} onChangeText={setAmount} placeholder="24900" keyboardType="decimal-pad"/><View style={styles.segment}><Pressable onPress={()=>setCadence("weekly")} style={[styles.segmentBtn,cadence==="weekly"&&styles.segmentActive]}><Text style={styles.segmentText}>Weekly</Text></Pressable><Pressable onPress={()=>setCadence("monthly")} style={[styles.segmentBtn,cadence==="monthly"&&styles.segmentActive]}><Text style={styles.segmentText}>Monthly</Text></Pressable><Pressable onPress={()=>setCadence("quarterly")} style={[styles.segmentBtn,cadence==="quarterly"&&styles.segmentActive]}><Text style={styles.segmentText}>Quarterly</Text></Pressable></View><Pressable onPress={create} style={styles.primary}><Text style={styles.primaryText}>Create schedule</Text></Pressable></View>
    <Text style={[styles.kicker,{marginTop:26,marginBottom:10}]}>SCHEDULES</Text>{loading?<Loader/>:items.length?items.map(x=><View key={x.id} style={styles.invoice}><View style={styles.invoiceTop}><View style={{flex:1}}><Text style={styles.invoiceCustomer}>{x.customer_name}</Text><Text style={styles.invoiceNo}>₹{x.amount} · {x.cadence} · next {x.next_issue_date}</Text></View><Text style={[styles.badge,x.active&&styles.badgeActive]}>{x.active?"Active":"Paused"}</Text></View><View style={styles.invoiceBottom}><Text style={styles.smallMuted}>{x.due_days} days to due</Text><Pressable onPress={async()=>{try{await api.toggleRecurring(x.id);load()}catch(e:any){Alert.alert("Could not update",e?.message||"Please try again.");}}}><Text style={styles.link}>{x.active?"Pause":"Resume"}</Text></Pressable></View></View>):<Empty title="No schedules yet" text="Create a recurring invoice schedule above."/>}
  </ScrollView>;
}

function More({onLogout,go}:{onLogout:()=>void;go:(tab:Tab)=>void}){
  const [user,setUser]=useState<any>(null); useEffect(()=>{api.me().then(setUser).catch(()=>{});},[]);
  return <ScrollView style={styles.screen} contentContainerStyle={styles.content}><Text style={styles.kicker}>WORKSPACE</Text><Text style={styles.title}>More</Text><Text style={styles.muted}>Workspace controls and production tools.</Text>
    <View style={[styles.card,{marginTop:18}]}>
      <Text style={styles.label}>Company</Text><Text style={styles.settingValue}>{user?.company_name||"—"}</Text>
      <Text style={styles.label}>Account</Text><Text style={styles.settingValue}>{user?.email||"—"}</Text>
      <Text style={styles.label}>Role</Text><Text style={styles.settingValue}>{user?.role||"—"}</Text>
      <Text style={styles.label}>Plan</Text><Text style={styles.settingValue}>{user?.subscription_state==="trial"?`Free trial · ${user?.trial_days_left||0} days left`:user?.subscription_plan||"Not active"}</Text><Pressable onPress={()=>go("recurring")} style={styles.secondaryBtn}><Text style={styles.secondaryText}>Recurring invoices</Text></Pressable><Pressable onPress={()=>Linking.openURL("https://recoverflow-7vnr.onrender.com/billing")} style={styles.secondaryBtn}><Text style={styles.secondaryText}>Manage subscription</Text></Pressable>
      <Pressable onPress={async()=>{try{const r=await api.testNotification();Alert.alert("Notification test",r.sent?"Test sent.":"No active device token yet.")}catch(e:any){Alert.alert("Notification test failed",e?.message||"Please try again.");}}} style={styles.secondaryBtn}><Text style={styles.secondaryText}>Test push notifications</Text></Pressable>
      <Pressable onPress={()=>Linking.openURL("https://recoverflow-7vnr.onrender.com")} style={styles.secondaryBtn}><Text style={styles.secondaryText}>Open RecoverFlow web</Text></Pressable>
    </View>
    <Pressable onPress={async()=>{await api.logout();onLogout();}} style={styles.dangerBtn}><Text style={styles.dangerText}>Sign out</Text></Pressable>
  </ScrollView>;
}

export default function App(){
  const [authed,setAuthed]=useState<boolean|null>(null); const [tab,setTab]=useState<Tab>("home");
  useEffect(()=>{api.hasToken().then(setAuthed)},[]);
  useEffect(()=>attachNotificationListeners(data=>{if(data?.screen==="invoices")setTab("invoices");else if(data?.screen==="customers")setTab("customers");else setTab("home");}),[]);
  useEffect(()=>{if(authed)registerPushNotifications()},[authed]);
  if(authed===null)return <Loader/>;
  if(!authed)return <Auth onDone={()=>setAuthed(true)}/>;
  const screen=tab==="home"?<Home go={setTab}/>:tab==="invoices"?<Invoices/>:tab==="add"?<AddInvoice onDone={()=>setTab("invoices")}/>:tab==="customers"?<Customers/>:tab==="recurring"?<Recurring onBack={()=>setTab("more")}/>:<More onLogout={()=>setAuthed(false)} go={setTab}/>;
  return <SafeAreaView style={styles.app}><StatusBar style="light"/>{screen}<View style={styles.tabbar}>{(["home","invoices","add","customers","more"] as Tab[]).map(t=><Pressable key={t} onPress={()=>setTab(t)} style={styles.tab}><Text style={[styles.tabIcon,t===tab&&styles.tabActive]}>{t==="home"?"⌂":t==="invoices"?"▤":t==="add"?"+":t==="customers"?"◉":"•••"}</Text><Text style={[styles.tabLabel,t===tab&&styles.tabActive]}>{t==="home"?"Home":t==="invoices"?"Invoices":t==="add"?"Add":t==="customers"?"Customers":"More"}</Text></Pressable>)}</View></SafeAreaView>;
}

const styles=StyleSheet.create({
  app:{flex:1,backgroundColor:colors.bg},screen:{flex:1,backgroundColor:colors.bg},content:{padding:20,paddingBottom:115},
  auth:{flex:1,backgroundColor:colors.bg},center:{flex:1},authScroll:{padding:24,paddingTop:70,paddingBottom:40,minHeight:"100%",justifyContent:"center"},
  brandRow:{flexDirection:"row",alignItems:"center",gap:10},logo:{height:40,width:40,borderRadius:12,backgroundColor:colors.cyan,alignItems:"center",justifyContent:"center"},logoText:{fontSize:22,fontWeight:"900",color:colors.bg},brand:{fontSize:20,fontWeight:"900",color:colors.text},
  kicker:{fontSize:11,fontWeight:"800",letterSpacing:1.5,color:colors.cyan},hero:{fontSize:46,fontWeight:"900",lineHeight:48,color:colors.text,marginTop:14},authSub:{fontSize:16,lineHeight:24,color:colors.muted,marginTop:14,marginBottom:24},
  card:{backgroundColor:colors.surface,borderWidth:1,borderColor:colors.line,borderRadius:radius.xl,padding:18},segment:{flexDirection:"row",backgroundColor:colors.surface2,borderRadius:12,padding:4,marginBottom:18},segmentBtn:{flex:1,paddingVertical:10,borderRadius:9,alignItems:"center"},segmentActive:{backgroundColor:colors.white},segmentText:{fontWeight:"800",color:colors.bg},
  fieldWrap:{marginBottom:14},label:{fontSize:12,fontWeight:"700",color:colors.muted,marginBottom:7},input:{height:50,borderWidth:1,borderColor:colors.line,borderRadius:12,paddingHorizontal:14,color:colors.text,backgroundColor:"#081522"},
  primary:{height:52,borderRadius:14,backgroundColor:colors.white,alignItems:"center",justifyContent:"center",marginTop:6},primaryText:{fontSize:15,fontWeight:"900",color:colors.bg},foot:{fontSize:11,textAlign:"center",color:"#587084",marginTop:18},
  title:{fontSize:30,fontWeight:"900",color:colors.text,marginTop:7},muted:{fontSize:14,lineHeight:21,color:colors.muted,marginTop:5},heroCard:{borderRadius:24,padding:22,marginTop:22,borderWidth:1,borderColor:"#245079"},cardKicker:{fontSize:11,fontWeight:"800",letterSpacing:1.2,color:colors.cyan},money:{fontSize:18,fontWeight:"900",color:colors.text},moneyLarge:{fontSize:34,letterSpacing:-1},heroRow:{flexDirection:"row",justifyContent:"space-between",marginTop:24},smallMuted:{fontSize:11,color:colors.muted,marginBottom:4},whiteBig:{fontSize:20,fontWeight:"900",color:colors.text},statsGrid:{flexDirection:"row",gap:12,marginTop:12},stat:{flex:1,backgroundColor:colors.surface,borderWidth:1,borderColor:colors.line,borderRadius:18,padding:16},statLabel:{fontSize:12,color:colors.muted,marginBottom:7},statTone:{fontSize:10,color:"#587084",marginTop:6},actionStack:{marginTop:14,gap:10},actionCard:{backgroundColor:colors.surface,borderWidth:1,borderColor:colors.line,borderRadius:18,padding:18,flexDirection:"row",alignItems:"center",justifyContent:"space-between"},actionTitle:{fontSize:15,fontWeight:"900",color:colors.text},arrow:{fontSize:24,color:colors.cyan},
  invoice:{backgroundColor:colors.surface,borderWidth:1,borderColor:colors.line,borderRadius:18,padding:16,marginBottom:10},invoiceTop:{flexDirection:"row",alignItems:"center",gap:10},invoiceCustomer:{fontSize:15,fontWeight:"800",color:colors.text},invoiceNo:{fontSize:11,color:colors.muted,marginTop:3},invoiceBottom:{marginTop:14,flexDirection:"row",alignItems:"center",justifyContent:"space-between",gap:12},rowActions:{flexDirection:"row",gap:12,alignItems:"center",flexWrap:"wrap",justifyContent:"flex-end"},badge:{fontSize:10,fontWeight:"800",color:colors.cyan,backgroundColor:"#123047",paddingHorizontal:9,paddingVertical:6,borderRadius:99},badgeDanger:{color:colors.danger,backgroundColor:"#3A1621"},badgeActive:{color:colors.success,backgroundColor:"#103427"},link:{fontSize:12,fontWeight:"800",color:colors.cyan},
  customer:{backgroundColor:colors.surface,borderWidth:1,borderColor:colors.line,borderRadius:18,padding:16,marginBottom:10},empty:{padding:35,alignItems:"center"},emptyTitle:{fontSize:18,fontWeight:"800",color:colors.text,marginBottom:5},settingValue:{fontSize:15,color:colors.text,fontWeight:"700",marginBottom:16},
  secondaryBtn:{marginTop:12,borderWidth:1,borderColor:colors.line,backgroundColor:colors.surface2,borderRadius:14,height:52,alignItems:"center",justifyContent:"center"},secondaryText:{fontWeight:"900",color:colors.cyan},dangerBtn:{marginTop:14,borderWidth:1,borderColor:"#5A2632",backgroundColor:"#2A1119",borderRadius:14,height:52,alignItems:"center",justifyContent:"center"},dangerText:{fontWeight:"900",color:colors.danger},
  loader:{flex:1,backgroundColor:colors.bg,alignItems:"center",justifyContent:"center",gap:10},tabbar:{position:"absolute",left:12,right:12,bottom:12,height:70,borderRadius:22,backgroundColor:"#0A1625",borderWidth:1,borderColor:colors.line,flexDirection:"row",alignItems:"center",justifyContent:"space-around"},tab:{alignItems:"center",justifyContent:"center",flex:1},tabIcon:{fontSize:19,color:"#587084",fontWeight:"900"},tabLabel:{fontSize:10,color:"#587084",marginTop:2,fontWeight:"700"},tabActive:{color:colors.cyan}
});
