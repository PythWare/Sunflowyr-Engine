# Sunflowyr Engine

Sunflowyr Engine is an upcoming high end modding toolkit for RWBY Grimm Eclipse.

It's a modding engine i'm developing to make modding RWBY GE significantly easier and pleasant to use. It's RWBY inspired.

Sunflowyr Engine will also include a Mod Creator and deluxe Mod Manager (unlike any other Mod Manager, it's called Beacon Studies) for usage as well. The Mod Creator will turn modded files into .RWBYGEP (mod package) or .RWBYGEI (mod installer) files to be used with the Mod Manager, they're custom mod formats I designed. RWBYGEP is ideal for mods that don't require choosing what to install while RWBYGEI is preferable when modders want to allow gamers to choose what parts of the mod to install.

# Details

It unpacks everything from the RWBY GE containers (all 400k+ files), assigns taildata (a method I use for safe mod appending/disabling) to an external json, converts various formats to easier to edit formats (FBX, GLB, PNG, wav, etc) but it still keeps the original Unity formats incase modders want to work with the Unity format versions, etc.

Its purpose is to unpack everything, ensure modded files can be properly applied to the containers by appending mods and auto updating metadata, and ensure safe mod disabling.

There is no size limits for mods, you aren't required to keep mods the same size as the original files, dynamic file sizes are supported so whether your mods are smaller/larger doesn't matter.

# The workflow for modders/mod users

The modding workflow for modders will be like this:

Unpack the game, mod whatever files, turn modded files into mod packages/installers (custom mod formats I designed which are .RWBYGEP and RWBYGEI) with the Mod Creator (similar to my Aldnoah Engine where you turn modded files into compatible mod formats with the Mod Manager), and then apply/disable as desired with the Mod Manager

The modding workflow for users that don't want to mod but just use mods will be like this:

Download mods you want, place the mods in the Mods folder, and apply/disable as desired.

# GUI examples

<img width="1486" height="718" alt="mod4" src="https://github.com/user-attachments/assets/8183d2a3-8b35-44c2-9354-a25185869b23" />

<img width="1496" height="780" alt="mod5" src="https://github.com/user-attachments/assets/069e6607-7b20-4d18-9894-f7906e50078f" />

<img width="1580" height="926" alt="mod1" src="https://github.com/user-attachments/assets/2221a929-ba48-4589-8019-923f2d64e3ab" />

<img width="1579" height="933" alt="mod3" src="https://github.com/user-attachments/assets/96f1c62f-0878-4274-94e3-2aec8fe359e0" />

<img width="1509" height="716" alt="mod6" src="https://github.com/user-attachments/assets/bc2e3eee-adbf-4862-82c8-6e5d31760ec7" />

<img width="1425" height="705" alt="mod7" src="https://github.com/user-attachments/assets/d689cb7c-49d4-4f94-8520-f5310aaf64ed" />

<img width="1457" height="748" alt="mod8" src="https://github.com/user-attachments/assets/4db38481-9efc-4280-a028-a2af44285a1a" />

<img width="1400" height="718" alt="mod9" src="https://github.com/user-attachments/assets/d66ab1f3-564c-4d09-b711-e278f9c86b39" />

<img width="1431" height="693" alt="mod10" src="https://github.com/user-attachments/assets/fd366b75-e8d4-4895-8feb-b10bfe8e81aa" />

<img width="1421" height="702" alt="mod11" src="https://github.com/user-attachments/assets/724e5a17-d179-429f-9578-aa86588a1cd0" />

<img width="1383" height="719" alt="mod12" src="https://github.com/user-attachments/assets/d45e4d6f-61b1-4b78-9cee-ac875091b388" />
