import {Platform} from "react-native";
import Constants from "expo-constants";
import * as Notifications from "expo-notifications";
import {registerNotificationToken} from "./api";

Notifications.setNotificationHandler({
  handleNotification: async()=>({
    shouldShowBanner:true,
    shouldShowList:true,
    shouldPlaySound:false,
    shouldSetBadge:true
  })
});

export async function registerPushNotifications(){
  if(Platform.OS==="web") return false;
  try{
    if(Platform.OS==="android"){
      await Notifications.setNotificationChannelAsync("default",{
        name:"RecoverFlow",
        importance:Notifications.AndroidImportance.DEFAULT,
        vibrationPattern:[0,200,100,200]
      });
    }
    const existing=await Notifications.getPermissionsAsync();
    let status=existing.status;
    if(status!=="granted"){
      const next=await Notifications.requestPermissionsAsync();
      status=next.status;
    }
    if(status!=="granted") return false;
    const projectId=Constants?.expoConfig?.extra?.eas?.projectId??Constants?.easConfig?.projectId;
    if(!projectId) return false;
    const token=(await Notifications.getExpoPushTokenAsync({projectId})).data;
    await registerNotificationToken(token,Platform.OS);
    return true;
  }catch{return false;}
}

export function attachNotificationListeners(onOpen:(data:any)=>void){
  const received=Notifications.addNotificationReceivedListener(()=>{});
  const response=Notifications.addNotificationResponseReceivedListener(r=>onOpen(r.notification.request.content.data||{}));
  return ()=>{received.remove();response.remove();};
}