# Sunflowyr Engine

Sunflowyr Engine is an upcoming high end modding toolkit for RWBY Grimm Eclipse. Scroll to bottom if you want to see GUI examples

It's a modding engine i'm developing to make modding RWBY GE significantly easier and pleasant to use. It's RWBY inspired.

Sunflowyr Engine will also include a Mod Creator, deluxe Mod Manager (unlike any other Mod Manager, it's called Beacon Studies), and AssetBundle Generator (used for generating new AssetBundle files for RWBY GE to load, will be explained further down in AssetBundle Generator section) for usage as well. The Mod Creator will turn modded files into .RWBYGEP (mod package) or .RWBYGEI (mod installer) files to be used with the Mod Manager, they're custom mod formats I designed. RWBYGEP is ideal for mods that don't require choosing what to install while RWBYGEI is preferable when modders want to allow gamers to choose what parts of the mod to install.

# Details

It unpacks everything from the RWBY GE containers, assigns taildata (a method I use for safe mod appending/disabling) to an external json, converts various formats to easier to edit formats (FBX, GLB, PNG, wav, etc) but it still keeps the original Unity formats incase modders want to work with the Unity format versions, etc.

Its purpose is to unpack everything, ensure modded files can be properly applied to the containers by appending mods and auto updating metadata, ensure safe mod disabling, and support AssetBundle generating for new assets being used ingame without having to replace existing files/assets.

There is no size limits for mods, you aren't required to keep mods the same size as the original files, dynamic file sizes are supported so whether your mods are smaller/larger doesn't matter.

# The workflow for modders/mod users

The modding workflow for modders will be like this:

Unpack the game, mod whatever files, turn modded files into mod packages/installers (custom mod formats I designed which are .RWBYGEP and RWBYGEI) with the Mod Creator (similar to my Aldnoah Engine where you turn modded files into compatible mod formats with the Mod Manager), and then apply/disable as desired with the Mod Manager or if going the AssetBundle route you'd select the files you want included and click Build AssetBundle (further details in AssetBundle section),

The modding workflow for users that don't want to mod but just use mods will be like this:

Download mods you want, place the mods in the Mods folder, and apply/disable as desired.

# AssetBundle Generator

Sunflowyr Engine's AssetBundle Generator can create valid AssetBundles for RWBY GE to use. What that means is you can make mods by building an AssetBundle without needing Unity installed or you can replace existing assets through mod appending (the Mod Creator/Beacon Studies route). To make use of AssetBundles mods, BepInEx will be needed. 

The quick explanation on AssetBundle vs Mod Appending is if you create a new AssetBundle, it allows you to bundle the new assets you want applied to the game without having to replace existing assets. However, BepInEx is needed if you choose to use AssetBundle mods since the game by default wouldn't know to load new AssetBundle files without BepInEx or rebuilding RWBY GE with Unity. If you don't want to apply new assets to a game and just want to mod the existing assets, it's better to do the Mod Appending approach (Mod Creator/Beacon Studies usage) because Sunflowyr Engine itself does not require BepInEx. 

So if you want to add new assets to RWBY GE (models/textures/playable characters/etc) without replacing existing assets go the AssetBundle Generator route, if you only want to mod existing assets that get unpacked and don't want extra dependencies then use Mod Creator/Beacon Studies.

# GUI examples

<img width="1451" height="803" alt="sunf1" src="https://github.com/user-attachments/assets/a7d6ea6f-69bc-43c0-a86a-bd4d796b39ba" />

<img width="1412" height="758" alt="sunf2" src="https://github.com/user-attachments/assets/1072a147-734a-4061-a1a9-971a267e45fc" />

<img width="1358" height="651" alt="sunf3" src="https://github.com/user-attachments/assets/0d7cfd8f-ab67-4d35-9697-1d09aea81263" />

<img width="1580" height="926" alt="mod1" src="https://github.com/user-attachments/assets/2221a929-ba48-4589-8019-923f2d64e3ab" />

<img width="1579" height="933" alt="mod3" src="https://github.com/user-attachments/assets/96f1c62f-0878-4274-94e3-2aec8fe359e0" />

<img width="1509" height="716" alt="mod6" src="https://github.com/user-attachments/assets/bc2e3eee-adbf-4862-82c8-6e5d31760ec7" />

<img width="1425" height="705" alt="mod7" src="https://github.com/user-attachments/assets/d689cb7c-49d4-4f94-8520-f5310aaf64ed" />

<img width="1457" height="748" alt="mod8" src="https://github.com/user-attachments/assets/4db38481-9efc-4280-a028-a2af44285a1a" />

<img width="1400" height="718" alt="mod9" src="https://github.com/user-attachments/assets/d66ab1f3-564c-4d09-b711-e278f9c86b39" />

<img width="1431" height="693" alt="mod10" src="https://github.com/user-attachments/assets/fd366b75-e8d4-4895-8feb-b10bfe8e81aa" />

<img width="1421" height="702" alt="mod11" src="https://github.com/user-attachments/assets/724e5a17-d179-429f-9578-aa86588a1cd0" />

<img width="1383" height="719" alt="mod12" src="https://github.com/user-attachments/assets/d45e4d6f-61b1-4b78-9cee-ac875091b388" />

<img width="1380" height="703" alt="mod13" src="https://github.com/user-attachments/assets/4aab9fcd-d05a-4c8b-b826-616a33db0a3d" />

<img width="1334" height="647" alt="mod14" src="https://github.com/user-attachments/assets/49a1d256-f5c1-4035-91fd-b76ddd75b069" />

# Mod examples

Yang's Shotgun particles changed to Lilac, Yang's ingame textures changed, and even a playable Katsuki bakugo mod example

<img width="618" height="581" alt="pa1" src="https://github.com/user-attachments/assets/98f4d815-4385-4f7d-be54-35c3360399d0" />

<img width="1280" height="732" alt="yang_6" src="https://github.com/user-attachments/assets/6ace6e99-3e88-4b74-b688-bb6e426a5ed2" />

<img width="1280" height="748" alt="bak4" src="https://github.com/user-attachments/assets/9c392e49-d297-439c-9238-7ee24f4b8216" />

<img width="1235" height="667" alt="bak5" src="https://github.com/user-attachments/assets/eeed5d70-1310-4efd-b6a4-d278ebc464fa" />

<img width="1232" height="675" alt="bak6" src="https://github.com/user-attachments/assets/cee208d0-7609-4853-8f5b-79f870f29fd9" />
