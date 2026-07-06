# Sunflowyr Engine

Sunflowyr Engine is an upcoming high end modding toolkit for RWBY Grimm Eclipse. Scroll to bottom if you want to see GUI examples

It's a modding engine i'm developing to make modding RWBY GE significantly easier and pleasant to use. It's RWBY inspired. Written in Python with the GUI using Tkinter (to keep the toolkit lightweight with minimal dependencies) but heavily customized to have a rad scroll-like GUI.

Sunflowyr Engine will also include a Mod Creator, deluxe Mod Manager (unlike any other Mod Manager, it's called Beacon Studies), Sunflowyr Morpher (model/texture porting tool, read further at the bottom of the readme), and AssetBundle Generator (used for generating new AssetBundle files for RWBY GE to load, will be explained further down in AssetBundle Generator section) for usage as well. The Mod Creator will turn modded files into .RWBYGEP (mod package) or .RWBYGEI (mod installer) files to be used with the Mod Manager, they're custom mod formats I designed. RWBYGEP is ideal for mods that don't require choosing what to install while RWBYGEI is preferable when modders want to allow gamers to choose what parts of the mod to install.

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

The quick explanation on AssetBundle vs Mod Appending is if you create a new AssetBundle, it allows you to bundle the new assets you want applied to the game without having to replace existing assets. However, BepInEx is needed if you choose to use AssetBundle mods since the game by default wouldn't know to load new AssetBundle files without BepInEx or rebuilding RWBY GE with Unity or even extending existing containers (something I may look at later on) to reference and load new files. If you don't want to apply new assets to a game and just want to mod the existing assets, it's better to do the Mod Appending approach (Mod Creator/Beacon Studies usage) because Sunflowyr Engine itself does not require BepInEx. 

So if you want to add new assets to RWBY GE (models/textures/playable characters/etc) without replacing existing assets go the AssetBundle Generator route, if you only want to mod existing assets that get unpacked and don't want extra dependencies then use Mod Creator/Beacon Studies.

# Sunflowyr Morpher

A lightweight tool i'm building for porting GLB/FBX models to RWBY GE, also supports auto converting the model texture the FBX/GLB uses to what RWBY GE can read.

# GUI examples

<img width="1365" height="672" alt="sunfl1" src="https://github.com/user-attachments/assets/635a74d4-0dce-413c-bef2-4dea40f93579" />

<img width="1367" height="682" alt="sunfl2" src="https://github.com/user-attachments/assets/38bc45ee-8cf4-4615-87c6-47ad29704f07" />

<img width="1358" height="671" alt="sunfl3" src="https://github.com/user-attachments/assets/9ca6799e-04b4-4686-a333-bab229d6bdd1" />

<img width="1580" height="926" alt="mod1" src="https://github.com/user-attachments/assets/2221a929-ba48-4589-8019-923f2d64e3ab" />

<img width="1579" height="933" alt="mod3" src="https://github.com/user-attachments/assets/96f1c62f-0878-4274-94e3-2aec8fe359e0" />

<img width="1509" height="716" alt="mod6" src="https://github.com/user-attachments/assets/bc2e3eee-adbf-4862-82c8-6e5d31760ec7" />

<img width="1425" height="705" alt="mod7" src="https://github.com/user-attachments/assets/d689cb7c-49d4-4f94-8520-f5310aaf64ed" />

<img width="1457" height="748" alt="mod8" src="https://github.com/user-attachments/assets/4db38481-9efc-4280-a028-a2af44285a1a" />

<img width="1548" height="962" alt="sunfl4" src="https://github.com/user-attachments/assets/b04e5135-1a92-4b77-9cc7-be4ea8fcda24" />

<img width="1517" height="947" alt="sunfl5" src="https://github.com/user-attachments/assets/0401055f-0175-40ac-b97c-61a8d69bd9e8" />

<img width="1521" height="961" alt="sunfl6" src="https://github.com/user-attachments/assets/b31e8d5b-53c0-4098-aba6-641b0649669b" />

<img width="1380" height="703" alt="mod13" src="https://github.com/user-attachments/assets/4aab9fcd-d05a-4c8b-b826-616a33db0a3d" />

<img width="1334" height="647" alt="mod14" src="https://github.com/user-attachments/assets/49a1d256-f5c1-4035-91fd-b76ddd75b069" />

<img width="1920" height="951" alt="morph1" src="https://github.com/user-attachments/assets/fba707e2-ba5c-4a23-9680-5a348f0429fe" />

<img width="1921" height="943" alt="morph2" src="https://github.com/user-attachments/assets/735ee566-ef50-407e-a06d-0e65cf99d45f" />

<img width="1921" height="938" alt="morph3" src="https://github.com/user-attachments/assets/6dcfeaa1-09be-46bb-974f-687735e3a68d" />

# Mod examples

Yang's Shotgun particles changed to Lilac, Yang's ingame textures changed, and even playable Katsuki bakugo/All Might/Izuku Midoriya mod examples

<img width="618" height="581" alt="pa1" src="https://github.com/user-attachments/assets/98f4d815-4385-4f7d-be54-35c3360399d0" />

<img width="1280" height="732" alt="yang_6" src="https://github.com/user-attachments/assets/6ace6e99-3e88-4b74-b688-bb6e426a5ed2" />

<img width="1280" height="748" alt="bak4" src="https://github.com/user-attachments/assets/9c392e49-d297-439c-9238-7ee24f4b8216" />

<img width="1235" height="667" alt="bak5" src="https://github.com/user-attachments/assets/eeed5d70-1310-4efd-b6a4-d278ebc464fa" />

<img width="1232" height="675" alt="bak6" src="https://github.com/user-attachments/assets/cee208d0-7609-4853-8f5b-79f870f29fd9" />

<img width="1281" height="751" alt="allmight2" src="https://github.com/user-attachments/assets/269cd95d-dfed-405c-bfb9-3eae952d24c5" />

<img width="1234" height="718" alt="allmight4" src="https://github.com/user-attachments/assets/e8b66130-7c7e-4858-b813-2f4b98677c1f" />

<img width="1280" height="747" alt="izuku1" src="https://github.com/user-attachments/assets/17faef0a-dd15-41e4-a764-3299c6d08670" />
